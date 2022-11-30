import logging
import getpass

from collections import defaultdict

from odoo import models, fields, api
from odoo.tools import config
from ..common import fqdn, local_pgadmin_cursor, os, list_local_dbs, local_pg_cursor
from ..container import docker_build

_logger = logging.getLogger(__name__)

forced_host_name = None


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
        'Number of max paralel build',
        default=lambda self: self.env['ir.config_parameter'].sudo().get_param('runbot.runbot_workers', default=2),
        tracking=True
    )
    nb_testing = fields.Integer(compute='_compute_nb')
    nb_running = fields.Integer(compute='_compute_nb')
    last_exception = fields.Char('Last exception')
    exception_count = fields.Integer('Exception count')
    psql_conn_count = fields.Integer('SQL connections count', default=0)

    def _compute_nb(self):
        groups = self.env['runbot.build'].read_group(
            [('host', 'in', self.mapped('name')), ('local_state', 'in', ('testing', 'running'))],
            ['host', 'local_state'],
            ['host', 'local_state'],
            lazy=False
        )
        count_by_host_state = {host.name: {} for host in self}
        for group in groups:
            count_by_host_state[group['host']][group['local_state']] = group['__count']
        for host in self:
            host.nb_testing = count_by_host_state[host.name].get('testing', 0)
            host.nb_running = count_by_host_state[host.name].get('running', 0)

    @api.model_create_single
    def create(self, values):
        if 'disp_name' not in values:
            values['disp_name'] = values['name']
        return super().create(values)

    def _bootstrap_local_logs_db(self):
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
                        message text NOT NULL);
                    """)
            except Exception as e:
                _logger.exception('Failed to create local logs database: %s', e)

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
        static_path = self._get_work_path()
        static_dirs = {d: os.path.join(static_path, d) for d in dirs}
        for dir, path in static_dirs.items():
            os.makedirs(path, exist_ok=True)
        self._bootstrap_db_template()
        self._bootstrap_local_logs_db()

    def _docker_build(self):
        """ build docker images needed by locally pending builds"""
        _logger.info('Building docker images...')
        self.ensure_one()
        static_path = self._get_work_path()
        self.clear_caches()  # needed to ensure that content is updated on all hosts
        for dockerfile in self.env['runbot.dockerfile'].search([('to_build', '=', True)]):
            self._docker_build_dockerfile(dockerfile, static_path)

    def _docker_build_dockerfile(self, dockerfile, workdir):
        _logger.info('Building %s, %s', dockerfile.name, hash(str(dockerfile.dockerfile)))
        docker_build_path = os.path.join(workdir, 'docker', dockerfile.image_tag)
        os.makedirs(docker_build_path, exist_ok=True)

        user = getpass.getuser()

        docker_append = f"""
            RUN groupadd -g {os.getgid()} {user} \\
            && useradd -u {os.getuid()} -g {user} -G audio,video {user} \\
            && mkdir /home/{user} \\
            && chown -R {user}:{user} /home/{user}
            USER {user}
            ENV COVERAGE_FILE /data/build/.coverage
            """

        with open(os.path.join(docker_build_path, 'Dockerfile'), 'w') as Dockerfile:
            Dockerfile.write(dockerfile.dockerfile + docker_append)

        docker_build_success, msg = docker_build(docker_build_path, dockerfile.image_tag)
        if not docker_build_success:
            dockerfile.to_build = False
            dockerfile.message_post(body=f'Build failure:\n{msg}')
            self.env['runbot.runbot'].warning(f'Dockerfile build "{dockerfile.image_tag}" failed on host {self.name}')

    def _get_work_path(self):
        return os.path.abspath(os.path.join(os.path.dirname(__file__), '../static'))

    @api.model
    def _get_current(self):
        name = self._get_current_name()
        return self.search([('name', '=', name)]) or self.create({'name': name})

    @api.model
    def _get_current_name(self):
        return config.get('forced_host_name') or fqdn()

    def get_running_max(self):
        icp = self.env['ir.config_parameter']
        return int(icp.get_param('runbot.runbot_running_max', default=5))

    def set_psql_conn_count(self):
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

    def disable(self):
        """ Reserve host if possible """
        self.ensure_one()
        nb_hosts = self.env['runbot.host'].search_count([])
        nb_reserved = self.env['runbot.host'].search_count([('assigned_only', '=', True)])
        if nb_reserved < (nb_hosts / 2):
            self.assigned_only = True

    def _fetch_local_logs(self, build_ids=None):
        """ fetch build logs from local database """
        logs_db_name = self.env['ir.config_parameter'].get_param('runbot.logdb_name')
        try:
            with local_pg_cursor(logs_db_name) as local_cr:
                res = []
                where_clause = 'WHERE build_id IN (%s)' if build_ids else ''
                query = f"""
                        SELECT * 
                        FROM (
                                SELECT id, create_date, name, level, dbname, func, path, line, type, message, split_part(dbname, '-', 1)::integer as build_id 
                                FROM ir_logging
                                )
                            AS ir_logs 
                        {where_clause}
                    ORDER BY id
                    """
                local_cr.execute(query, build_ids)
                col_names = [col.name for col in local_cr.description]
                for row in local_cr.fetchall():
                    res.append({name:value for name,value in zip(col_names, row)})
                return res
        except Exception:
            return []

    def process_logs(self, build_ids=None):
        """move logs from host to the leader"""
        ir_logs = self._fetch_local_logs()
        logs_by_build_id = defaultdict(list)

        local_log_ids = []
        for log in ir_logs:
            if log['dbname'] and '-' in log['dbname']:
                logs_by_build_id[int(log['dbname'].split('-', maxsplit=1)[0])].append(log)
            else:
                local_log_ids.append(log['id'])

        builds = self.env['runbot.build'].browse(logs_by_build_id.keys())

        logs_to_send = []
        for build in builds.exists():
            log_counter = build.log_counter
            build_logs = logs_by_build_id[build.id]
            for ir_log in build_logs:
                local_log_ids.append(ir_log['id'])
                ir_log['active_step_id'] = build.active_step.id
                ir_log['type'] = 'server'
                log_counter -= 1
                if log_counter == 0:
                    ir_log['level'] = 'SEPARATOR'
                    ir_log['func'] = ''
                    ir_log['type'] = 'runbot'
                    ir_log['message'] = 'Log limit reached (full logs are still available in the log file)'
                elif log_counter < 0:
                    continue
                ir_log['build_id'] = build.id
                logs_to_send.append({k:ir_log[k] for k in ir_log if k != 'id'})
            build.log_counter = log_counter

        if logs_to_send:
            self.env['ir.logging'].create(logs_to_send)
        self.env.cr.commit()  # we don't want to remove local logs that were not inserted in main runbot db
        if local_log_ids:
            logs_db_name = self.env['ir.config_parameter'].get_param('runbot.logdb_name')
            with local_pg_cursor(logs_db_name) as local_cr:
                local_cr.execute("DELETE FROM ir_logging WHERE id in %s", [tuple(local_log_ids)])
