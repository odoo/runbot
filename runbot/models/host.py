import datetime
import json
import logging

from collections import defaultdict
from docker.errors import ImageNotFound

from odoo import models, fields, api
from odoo.tools import config, ormcache
from ..common import fqdn, local_pgadmin_cursor, os, list_local_dbs, local_pg_cursor
from ..container import docker_push, docker_pull, docker_prune, docker_images, docker_remove, docker_tag

_logger = logging.getLogger(__name__)


class Host(models.Model):
    _name = 'runbot.host'
    _description = "Host"
    _order = 'id'
    _inherit = 'mail.thread'

    name = fields.Char('Host name', required=True)
    disp_name = fields.Char('Display name')
    active = fields.Boolean('Active', default=True, tracking=True)
    last_start_loop = fields.Datetime('Last start')
    last_end_loop = fields.Datetime('Last end')
    last_success = fields.Datetime('Last success')
    assigned_only = fields.Boolean('Only accept assigned build', default=False, tracking=True)
    nb_worker = fields.Integer(
        'Number of max parallel build',
        default=lambda self: self.env['ir.config_parameter'].sudo().get_param('runbot.runbot_workers', default=2),
        tracking=True,
    )
    nb_run_slot = fields.Integer(
        'Number of max parallel running build',
        default=0,
        tracking=True,
    )

    nb_testing = fields.Integer(compute='_compute_nb')
    nb_running = fields.Integer(compute='_compute_nb')
    last_exception = fields.Char('Last exception')
    exception_count = fields.Integer('Exception count')
    psql_conn_count = fields.Integer('SQL connections count', default=0)
    host_message_ids = fields.One2many('runbot.host.message', 'host_id')
    build_ids = fields.One2many('runbot.build', compute='_compute_build_ids')

    paused = fields.Boolean('Paused', help='Host will stop scheduling while paused')
    profile = fields.Boolean('Profile', help='Enable profiling on this host')

    is_leader = fields.Boolean('Is leader', help='This host is the leader of the cluster', default=False)
    is_builder = fields.Boolean('Is builder', help='This host is a builder of the cluster', default=True)
    is_registry = fields.Boolean('Is docker registry', help='This host is a docker regisrty', default=False)
    is_backup = fields.Boolean('Is backup', help='This host backup the most important databases', default=False)
    send_status = fields.Boolean('Send status', help='If leader, this host will send status updates, disable to use the status service', default=True)

    use_remote_docker_registry = fields.Boolean('Use remote Docker Registry', default=False, help="Use docker registry for pulling images")
    docker_registry_url = fields.Char('Registry Url', help="Override global registry URL for this host.")

    def _compute_nb(self):
        # Array of tuple (host, state, count)
        groups = self.env['runbot.build']._read_group(
            [('host', 'in', self.mapped('name')), ('local_state', 'in', ('testing', 'running'))],
            ['host', 'local_state'],
            ['id:count'],
        )
        count_by_host_state = dict(((host, state), count) for host, state, count in groups)
        for host in self:
            host.nb_testing = count_by_host_state.get((host.name, 'testing'), 0)
            host.nb_running = count_by_host_state.get((host.name, 'running'), 0)

    def _compute_build_ids(self):
        for host in self:
            host.build_ids = self.env['runbot.build'].search([('host', '=', host.name), ('local_state', 'in', ('pending', 'testing', 'running'))])

    def _static_url(self):
        use_ssl = self.env['ir.config_parameter'].get_param('runbot.use_ssl', default=True)
        scheme = 'https' if use_ssl else 'http'
        return f'{scheme}://{self.name}/runbot/static/'

    def _build_url(self, build):
        return f'{self._static_url()}build/{build.dest}/'

    def _backup_url(self):
        return f'{self._static_url()}backups/'

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if 'disp_name' not in vals:
                vals['disp_name'] = vals['name']
        return super().create(vals_list)

    def _bootstrap_local_logs_db(self):
        # TODO cleanup remove
        """ bootstrap a local database that will collect logs from builds """
        logs_db_name = self.env['ir.config_parameter'].get_param('runbot.logdb_name')
        if logs_db_name not in list_local_dbs():
            _logger.info('Logging database %s not found. Creating it ...', logs_db_name)
            with local_pgadmin_cursor() as local_cr:
                local_cr.execute(f"""CREATE DATABASE "{logs_db_name}" TEMPLATE template0 LC_COLLATE 'C' ENCODING 'unicode'""")
            try:
                with local_pg_cursor(logs_db_name) as local_cr:
                    # create_date, type, dbname, name, level, message, path, line, func
                    local_cr.execute("""CREATE TABLE ir_logging (
                        id bigserial NOT NULL,
                        create_date timestamp without time zone,
                        name character varying NOT NULL,
                        level character varying,
                        dbname character varying,
                        func character varying NOT NULL,
                        path character varying NOT NULL,
                        line character varying NOT NULL,
                        type character varying NOT NULL,
                        metadata jsonb,
                        message text NOT NULL);
                    """)
            except Exception as e:
                _logger.exception('Failed to create local logs database: %s', e)
        else:
            # TODO cleanup remove in 20.0
            with local_pg_cursor(logs_db_name) as local_cr:
                local_cr.execute("""SELECT 1
                FROM information_schema.columns
                WHERE table_name='ir_logging' and column_name='metadata'""")
                if not local_cr.fetchone():
                    _logger.info('Adding metadata column to ir_logging table')
                    local_cr.execute("""ALTER TABLE ir_logging ADD COLUMN metadata jsonb""")


    def _bootstrap_db_template(self):
        """ boostrap template database if needed """
        icp = self.env['ir.config_parameter']
        db_template = icp.get_param('runbot.runbot_db_template', default='template0')
        if db_template and db_template != 'template0':
            with local_pgadmin_cursor() as local_cr:
                local_cr.execute("""SELECT datname FROM pg_catalog.pg_database WHERE datname = '%s';""" % db_template)
                res = local_cr.fetchone()
                if not res:
                    local_cr.execute("""CREATE DATABASE "%s" TEMPLATE template0 LC_COLLATE 'C' ENCODING 'unicode'""" % db_template)
                    # TODO UPDATE pg_database set datallowconn = false, datistemplate = true (but not enough privileges)

    def _bootstrap(self):
        """ Create needed directories in static """
        dirs = ['build', 'nginx', 'repo', 'sources', 'src', 'docker']
        static_path = self.env['runbot.runbot']._root()
        static_dirs = {d: self.env['runbot.runbot']._path(d) for d in dirs}
        for dir, path in static_dirs.items():
            os.makedirs(path, exist_ok=True)
        self._bootstrap_db_template()
        self._bootstrap_local_logs_db()

    def _get_docker_registry_url(self):
        if self.docker_registry_url:
            return self.docker_registry_url
        icp = self.env['ir.config_parameter']
        docker_registry_url = icp.get_param('runbot.docker_registry_url', default=None)
        if docker_registry_url:
            return docker_registry_url.strip('/')
        docker_registry_host = self.browse(int(icp.get_param('runbot.docker_registry_host_id', default=0)))
        if docker_registry_host:
            return f'dockerhub.{docker_registry_host.name}'.strip('/')

    def _docker_update_images(self):
        """ build docker images needed by locally pending builds"""
        self.ensure_one()
        icp = self.env['ir.config_parameter']
        docker_registry_host = self.browse(int(icp.get_param('runbot.docker_registry_host_id', default=0)))
        docker_registry_url = self._get_docker_registry_url()
        # pull all images from the runbot docker registry
        is_main_registry = docker_registry_host == self
        all_docker_files = self.env['runbot.dockerfile'].search([])
        all_tags = set(all_docker_files.mapped('image_tag'))
        if docker_registry_url and self.use_remote_docker_registry and not is_main_registry:
            _logger.info('Pulling docker images...')
            total_duration = 0
            for dockerfile in all_docker_files.filtered('always_pull'):
                remote_tag = f'{docker_registry_url}/{dockerfile.image_tag}'
                result = docker_pull(remote_tag)
                if result['success']:
                    result['image'].tag(dockerfile.image_tag)
                total_duration += result['duration']
                if total_duration > 60:
                    _logger.warning("Pulling images took more than 60 seconds... will continue later")
                    break
        else:
            _logger.info('Building docker images...')
            # as variants depend on their parent we want to build the parents first
            for dockerfile in self.env['runbot.dockerfile'].search([('to_build', '=', True)], order='parent_id nulls first'):
                future_identifier = None
                if not dockerfile.in_error:
                    future_identifier = dockerfile._build(self)
                if self.is_registry:
                    if future_identifier:
                        dockerfile.image_future_identifier = future_identifier
                        if dockerfile.auto_sync:
                            dockerfile.image_identifier = future_identifier
                    docker_tag(dockerfile.image_previous_identifier, dockerfile.image_previous_tag)
                    docker_tag(dockerfile.image_identifier, dockerfile.image_tag)
                    docker_tag(dockerfile.image_future_identifier, dockerfile.image_future_tag)
                    for tag in [dockerfile.image_tag, dockerfile.image_future_tag]:
                        try:
                            docker_push(tag)  # for now, always push locally
                            if is_main_registry:
                                docker_registry_url = self.docker_registry_url
                                if self.docker_registry_url:
                                    docker_registry_url = self.docker_registry_url
                                else:
                                    docker_registry_url = icp.get_param('runbot.docker_registry_url', default='').strip('/')
                                if docker_registry_url:
                                    docker_push(tag, docker_registry_url)
                        except ImageNotFound:
                            _logger.warning("Image tag `%s` not found. Skipping push", tag)
                else:
                    if future_identifier:
                        docker_tag(future_identifier, dockerfile.image_tag) # for a setup without registry
                        docker_tag(future_identifier, dockerfile.image_future_tag)

        _logger.info('Cleaning docker images...')
        for image in docker_images():
            for tag in image.tags:
                cleaned_tag = tag.removesuffix('.future').removesuffix('.previous')
                if tag.startswith('odoo:') and cleaned_tag not in all_tags:  # what about odoo:latest
                    _logger.info(f"Removing tag '{tag}' since it doesn't exist anymore")
                    docker_remove(tag)

        result = docker_prune()
        if result['ImagesDeleted']:
            for r in result['ImagesDeleted']:
                for operation, identifier in r.items():
                    _logger.info(f"{operation}: {identifier}")
        if result['SpaceReclaimed']:
            _logger.info(f"Space reclaimed: {result['SpaceReclaimed']}")
        _logger.info('Done...')

    @ormcache()
    def _host_list(self):
        return {host.name: host.id for host in self.search([])}

    def _get_host(self, name):
        return self.browse(self._host_list().get(name)) or self.with_context(active_test=False).search([('name', '=', name)])

    @api.model
    def _get_current(self):
        name = self._get_current_name()
        return self._get_host(name) or self.create({'name': name})

    @api.model
    def _get_current_name(self):
        return config.get('forced_host_name') or fqdn()

    def _get_running_max(self):
        icp = self.env['ir.config_parameter']
        return self.nb_run_slot or int(icp.get_param('runbot.runbot_running_max', default=5))

    def _set_psql_conn_count(self):
        _logger.info('Updating psql connection count...')
        self.ensure_one()
        with local_pgadmin_cursor() as local_cr:
            local_cr.execute("SELECT sum(numbackends) FROM pg_stat_database;")
            res = local_cr.fetchone()
        self.psql_conn_count = res and res[0] or 0

    def _total_testing(self):
        return sum(host.nb_testing for host in self)

    def _total_workers(self):
        return sum(host.nb_worker for host in self)

    def _disable(self):
        """ Reserve host if possible """
        self.ensure_one()
        nb_hosts = self.env['runbot.host'].search_count([])
        nb_reserved = self.env['runbot.host'].search_count([('assigned_only', '=', True)])
        if nb_reserved < (nb_hosts / 2):
            self.assigned_only = True

    def _fetch_local_logs(self, builds=None):
        res = []
        cleanups = []
        for build in builds:
            if build._is_file('odoo_log.seek'):
                new_seek = seek = int(build._read_file('odoo_log.seek'))
                log_file_path = 'logs/%s_logs.json' % build.active_step.sanitized_name(build)
                if not build._is_file(log_file_path):
                    continue
                with open(build._path(log_file_path), encoding='utf-8') as f:
                    f.seek(seek)
                    for line in f.readlines():
                        try:
                            json_log = json.loads(line)
                            log_data = {
                                'create_date': datetime.datetime.fromtimestamp(float(json_log['created'])) if json_log.get('created') else datetime.datetime.now(),
                                'level': str(json_log.get('levelname', 'INFO')),
                                'name': str(json_log.get('name', '')),
                                'dbname': str(json_log.get('dbname', '')),
                                'func': str(json_log.get('funcName', '')),
                                'path': str(json_log.get('pathname', '')),
                                'line': str(json_log.get('lineno', '')),
                                'type': 'server',
                                'message': str(json_log.get('message', '')),
                                'build_id': build.id,
                            }
                            if json_log.get('test'):
                                log_data['metadata'] = {'test': json_log.get('test')}

                            new_seek = f.tell()
                            res.append(log_data)
                        except (KeyError, json.JSONDecodeError):
                            _logger.exception('Failed to parse log line:')
                            break

                if new_seek != seek:
                    def cleanup(build=build, new_seek=new_seek):
                        build._write_file('odoo_log.seek', str(new_seek))
                    cleanups.append(cleanup)
                continue

            # TODO cleanup remove
            log_to_delete = []
            build_ids = build.ids
            logs_db_name = self.env['ir.config_parameter'].get_param('runbot.logdb_name')
            with local_pg_cursor(logs_db_name) as local_cr:
                where_clause = "WHERE split_part(dbname, '-', 1) IN %s" if build_ids else ''
                query = f"""
                        SELECT *
                        FROM (
                                SELECT id, create_date, name, level, dbname, func, path, line, type, message, metadata
                                FROM ir_logging
                                )
                            AS ir_logs
                        {where_clause}
                    ORDER BY id
                    LIMIT 10000
                    """
                build_ids = [tuple(str(build) for build in build_ids)]
                local_cr.execute(query, build_ids)
                col_names = [col.name for col in local_cr.description]
                for row in local_cr.fetchall():
                    vals = dict(zip(col_names, row))
                    res.append(vals)
                    log_to_delete.append(int(vals.pop('id')))
            if log_to_delete:
                def cleanup(log_to_delete=log_to_delete):
                    logs_db_name = self.env['ir.config_parameter'].get_param('runbot.logdb_name')
                    with local_pg_cursor(logs_db_name) as local_cr:
                        local_cr.execute("DELETE FROM ir_logging WHERE id in %s", [tuple(log_to_delete)])
                cleanups.append(cleanup)

        return res, cleanups

    def _process_logs(self, testing_builds):
        """move logs from host to the leader"""
        ir_logs, cleanups = self._fetch_local_logs(testing_builds)
        logs_by_build_id = defaultdict(list)

        local_log_ids = []
        for log in ir_logs:
            if not log.get('build_id'):  # TODO cleanup remove condition, not needed once using only json log
                try:
                    log['build_id'] = int(log['dbname'].split('-', maxsplit=1)[0])
                except (ValueError, AttributeError, KeyError):
                    if log.get('id'):
                        local_log_ids.append(log['id'])  # TODO cleanup remove not needed once using only json log
            if log.get('build_id'):
                logs_by_build_id[log['build_id']].append(log)

        logs_to_send = []
        for build in testing_builds:
            log_counter = build.log_counter
            build_logs = logs_by_build_id[build.id]
            for ir_log in build_logs:
                if 'id' in ir_log:  # TODO cleanup
                    local_log_ids.append(ir_log['id'])
                ir_log['type'] = 'server'
                log_counter -= 1
                if log_counter == 0:
                    ir_log['level'] = 'SEPARATOR'
                    ir_log['func'] = ''
                    ir_log['type'] = 'runbot'
                    ir_log['message'] = 'Log limit reached (full logs are still available in the log file)'
                elif log_counter < 0:
                    continue
                else:
                    if len(ir_log['message']) > 10000:
                        ir_log['message'] = ir_log['message'][:10000] + "\n ...<message too long, truncated>"

                ir_log['build_id'] = build.id
                logs_to_send.append(ir_log)
            build.log_counter = log_counter

        if logs_to_send:
            self.env['ir.logging'].create(logs_to_send)
        self.env.cr.commit()  # we don't want to remove local logs that were not inserted in main runbot db

        for cleanup in cleanups:
            cleanup()

    def _get_build_domain(self, domain=None):
        domain = domain or []
        return [('host', '=', self.name)] + domain

    def _get_builds(self, domain, order=None):
        return self.env['runbot.build'].search(self._get_build_domain(domain), order=order)

    def _process_messages(self):
        return self.host_message_ids._process()


class MessageQueue(models.Model):
    _name = 'runbot.host.message'
    _description = "Message queue"
    _order = 'id'
    _log_access = False

    create_date = fields.Datetime('Create date', default=fields.Datetime.now)
    host_id = fields.Many2one('runbot.host', required=True, ondelete='cascade', index=True)
    build_id = fields.Many2one('runbot.build', index=True)
    message = fields.Char('Message')

    def _process(self):
        records = self
        processed = False
        # todo consume messages here
        if records:
            processed = True
            for record in records:
                if record.message == 'kill':
                    if record.build_id:
                        build = record.build_id
                        result = None
                        if build.local_state != 'running' and build.global_result not in ('warn', 'ko'):
                            result = 'killed'
                        build._kill(result=result)
                else:
                    self.env['runbot.runbot']._warning(f'Host {record.host_id.name} got an unexpected message {record.message}')
        self.unlink()
        return processed
