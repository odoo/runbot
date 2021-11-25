import logging
from odoo import models, fields, api
from ..common import fqdn, local_pgadmin_cursor, os
from ..container import docker_build
_logger = logging.getLogger(__name__)


class Host(models.Model):
    _name = 'runbot.host'
    _description = "Host"
    _order = 'id'
    _inherit = 'mail.thread'

    name = fields.Char('Host name', required=True, unique=True)
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

    def _docker_build(self):
        """ build docker images needed by locally pending builds"""
        _logger.info('Building docker image...')
        self.ensure_one()
        static_path = self._get_work_path()
        self.clear_caches()  # needed to ensure that content is updated on all hosts
        for dockerfile in self.env['runbot.dockerfile'].search([('to_build', '=', True)]):
            _logger.info('Building %s, %s', dockerfile.name, hash(str(dockerfile.dockerfile)))
            docker_build_path = os.path.join(static_path, 'docker', dockerfile.image_tag)
            os.makedirs(docker_build_path, exist_ok=True)
            with open(os.path.join(docker_build_path, 'Dockerfile'), 'w') as Dockerfile:
                Dockerfile.write(dockerfile.dockerfile)
            build_process = docker_build(docker_build_path, dockerfile.image_tag)
            if build_process != 0:
                dockerfile.to_build = False
                message = 'Dockerfile build "%s" failed on host %s' % (dockerfile.image_tag, self.name)
                dockerfile.message_post(body=message)
                self.env['runbot.runbot'].warning(message)
                _logger.warning(message)

    def _get_work_path(self):
        return os.path.abspath(os.path.join(os.path.dirname(__file__), '../static'))

    @api.model
    def _get_current(self, suffix=''):
        name = '%s%s' % (fqdn(), suffix)
        return self.search([('name', '=', name)]) or self.create({'name': name})

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
