import logging
import os

from odoo import models, fields, api
from ..common import fqdn, local_pgadmin_cursor
from ..container import docker_build
_logger = logging.getLogger(__name__)


class RunboHost(models.Model):
    _name = "runbot.host"
    _description = "Host"
    _order = 'id'
    _inherit = 'mail.thread'

    name = fields.Char('Host name', required=True, unique=True)
    disp_name = fields.Char('Display name')
    active = fields.Boolean('Active', default=True)
    last_start_loop = fields.Datetime('Last start')
    last_end_loop = fields.Datetime('Last end')
    last_success = fields.Datetime('Last success')
    assigned_only = fields.Boolean('Only accept assigned build', default=False)
    nb_worker = fields.Integer('Number of max paralel build', help="0 to use icp value", default=0)
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
        if not 'disp_name' in values:
            values['disp_name'] = values['name']
        return super().create(values)

    def _bootstrap_db_template(self):
        """ boostrap template database if needed """
        icp = self.env['ir.config_parameter']
        db_template = icp.get_param('runbot.runbot_db_template', default='template1')
        if db_template and db_template != 'template1':
            with local_pgadmin_cursor() as local_cr:
                local_cr.execute("""SELECT datname FROM pg_catalog.pg_database WHERE datname = '%s';""" % db_template)
                res = local_cr.fetchone()
                if not res:
                    local_cr.execute("""CREATE DATABASE "%s" TEMPLATE template1 LC_COLLATE 'C' ENCODING 'unicode'""" % db_template)
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
        """ build docker image """
        static_path = self._get_work_path()
        log_path = os.path.join(static_path, 'docker', 'docker_build.txt')
        docker_build(log_path, static_path)

    def _get_work_path(self):
        return os.path.abspath(os.path.join(os.path.dirname(__file__), '../static'))

    @api.model
    def _get_current(self):
        name = fqdn()
        return self.search([('name', '=', name)]) or self.create({'name': name})

    def get_nb_worker(self):
        icp = self.env['ir.config_parameter']
        return self.nb_worker or int(icp.sudo().get_param('runbot.runbot_workers', default=6))

    def get_running_max(self):
        icp = self.env['ir.config_parameter']
        return int(icp.get_param('runbot.runbot_running_max', default=75))

    def set_psql_conn_count(self):
        _logger.debug('Updating psql connection count...')
        self.ensure_one()
        with local_pgadmin_cursor() as local_cr:
            local_cr.execute("SELECT sum(numbackends) FROM pg_stat_database;")
            res = local_cr.fetchone()
        self.psql_conn_count = res and res[0] or 0

    def _total_testing(self):
        return sum(host.nb_testing for host in self)

    def _total_workers(self):
        return sum(host.get_nb_worker() for host in self)

    def disable(self):
        """ Reserve host if possible """
        self.ensure_one()
        nb_hosts = self.env['runbot.host'].search_count([])
        nb_reserved = self.env['runbot.host'].search_count([('assigned_only', '=', True)])
        if nb_reserved < (nb_hosts / 2):
            self.assigned_only = True
