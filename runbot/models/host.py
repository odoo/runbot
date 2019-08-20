import logging
from odoo import models, fields, api
from ..common import fqdn
_logger = logging.getLogger(__name__)


class RunboHost(models.Model):
    _name = "runbot.host"
    _order = 'id'

    name = fields.Char('Host name', required=True, unique=True)
    display_name = fields.Char('Display name')
    active = fields.Boolean('Active', default=True)
    last_start_loop = fields.Datetime('Last')
    last_end_loop = fields.Datetime('Last')
    last_success = fields.Datetime('Last')
    assigned_only = fields.Boolean('Only accept assigned build', default=False)
    nb_worker = fields.Integer('Number of max paralel build', help="0 to use icp value", default=0)
    nb_testing = fields.Integer(compute='_compute_nb')
    nb_running = fields.Integer(compute='_compute_nb')

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
            host.nb_testing = count_by_host_state[self.name].get('testing', 0)
            host.nb_running = count_by_host_state[self.name].get('running', 0)

    @api.model
    def create(self, values):
        if not 'display_name' in values:
            values['display_name'] = values['name']
        return super().create(values)

    @api.model
    def _get_current(self):
        name = fqdn()
        return self.search([('name', '=', name)]) or self.create({'name': name})

    def get_nb_worker(self):
        icp = self.env['ir.config_parameter']
        return self.nb_worker or int(icp.get_param('runbot.runbot_workers', default=6))

