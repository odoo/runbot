import datetime
import json
import logging
import platform
import shutil

import psutil

from collections import defaultdict
from docker.errors import ImageNotFound

from odoo import models, fields, api
from odoo.tools import config, ormcache
from ..common import fqdn, os
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
    host_message_ids = fields.One2many('runbot.host.message', 'host_id')
    build_ids = fields.One2many('runbot.build', compute='_compute_build_ids')

    paused = fields.Boolean('Paused', help='Host will stop scheduling while paused', tracking=True)
    profile = fields.Boolean('Profile', help='Enable profiling on this host')

    is_leader = fields.Boolean('Is leader', help='This host is the leader of the cluster', default=False)
    is_builder = fields.Boolean('Is builder', help='This host is a builder of the cluster', default=True)
    is_registry = fields.Boolean('Is docker registry', help='This host is a docker regisrty', default=False)
    is_backup = fields.Boolean('Is backup', help='This host backup the most important databases', default=False)
    is_assigner = fields.Boolean('Is assigner', help='This host will assign builds to other hosts', default=False)
    send_status = fields.Boolean('Send status', help='If leader, this host will send status updates, disable to use the status service', default=True)

    use_remote_docker_registry = fields.Boolean('Use remote Docker Registry', default=False, help="Use docker registry for pulling images")
    docker_registry_url = fields.Char('Registry Url', help="Override global registry URL for this host.")

    cpu_count = fields.Integer('CPU count', default=0)
    memory = fields.Integer('Memory (GiB)', help="Host Total memory (GiB)", default=0)
    os_version = fields.Char('OS Version')
    disk_size = fields.Integer('Disk Size (GiB)', help="Runbot Partition Size", default=0)
    disk_used = fields.Integer('Disk Used (GiB)', help="Runbot Partition Used Space", default=0)
    disk_usage = fields.Char('Disk Usage', compute='_compute_disk_usage')

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

    @api.depends('disk_used', 'disk_size')
    def _compute_disk_usage(self):
        for host in self:
            host.disk_usage = f'{host.disk_used} / {host.disk_size} GiB' if host.disk_size else False

    def _static_url(self):
        use_ssl = self.env['ir.config_parameter'].sudo().get_param('runbot.use_ssl', default=True)
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

    def _bootstrap(self):
        for dir in ['build', 'nginx', 'repo', 'sources', 'src', 'docker']:
            os.makedirs(self.env['runbot.runbot']._path(dir), exist_ok=True)

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
            for dockerfile in self.env['runbot.dockerfile'].search([('to_build', '=', True), ('is_template', '=', False)], order='parent_id nulls first'):
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
                    tags_to_push = [dockerfile.image_tag, dockerfile.image_future_tag]
                    docker_registry_url = self._get_docker_registry_url()
                    if docker_registry_url:
                        self.env.cr.commit()
                        for tag in tags_to_push:
                            try:
                                if is_main_registry or docker_registry_url == self.docker_registry_url:
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

    def _get_disk_usage(self):
        return shutil.disk_usage(self.env['runbot.runbot']._path())

    def _set_host_infos(self):
        self.ensure_one()
        self.cpu_count = psutil.cpu_count() or 0
        self.memory = round(psutil.virtual_memory().total / (1024**3))
        self.os_version = platform.freedesktop_os_release().get('PRETTY_NAME', False)
        self.disk_size = round(self._get_disk_usage().total / 1024**3)

    def _total_testing(self):
        return self.env['runbot.build'].search_count([('local_state', '=', 'testing')])

    def _total_workers(self):
        [(total,)] = self.env['runbot.host']._read_group([('assigned_only', '=', False)], [], ['nb_worker:sum'])
        return total

    def _disable(self):
        """ Reserve host if possible """
        self.ensure_one()
        nb_hosts = self.env['runbot.host'].search_count([])
        nb_reserved = self.env['runbot.host'].search_count([('assigned_only', '=', True)])
        if nb_reserved < (nb_hosts / 2):
            self.assigned_only = True

    def _fetch_local_logs(self, builds):
        res = defaultdict(list)
        cleanups = []
        for build in builds:
            if build._is_file('odoo_log.seek'):
                new_seek = seek = int(build._read_file('odoo_log.seek'))
                log_file_path = 'logs/%s_logs.json' % build.active_step.sanitized_name(build)
                if not build._is_file(log_file_path):
                    continue
                with open(build._path(log_file_path), encoding='utf-8') as f:
                    f.seek(seek)
                    faketime_offset = build._get_faketime_offset() or datetime.timedelta()
                    for line in f.readlines():
                        try:
                            json_log = json.loads(line)
                            log_data = {
                                'create_date': datetime.datetime.fromtimestamp(float(json_log['created'])) - faketime_offset if json_log.get('created') else datetime.datetime.now(),
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
                            if json_log.get('exc_info'):
                                log_data['message'] = f'{log_data["message"]}\n{json_log["exc_info"]}'

                            new_seek = f.tell()
                            res[build.id].append(log_data)
                        except (KeyError, json.JSONDecodeError):
                            _logger.exception('Failed to parse log line:')
                            break

                if new_seek != seek:
                    def cleanup(build=build, new_seek=new_seek):
                        build._write_file('odoo_log.seek', str(new_seek))
                    cleanups.append(cleanup)

        return res, cleanups

    def _process_logs(self, testing_builds):
        """move logs from host to the leader"""
        logs_by_build_id, cleanups = self._fetch_local_logs(testing_builds)
        logs_to_send = []
        for build in testing_builds:
            log_counter = build.log_counter
            build_logs = logs_by_build_id.get(build.id, [])
            for ir_log in build_logs:
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

    def _action_pause(self):
        for host in self:
            host.paused = True

    def _action_unpause(self):
        for host in self:
            host.paused = False


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
