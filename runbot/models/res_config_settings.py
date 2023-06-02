# -*- coding: utf-8 -*-
import re

from .. import common
from odoo import api, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    runbot_workers = fields.Integer('Default number of workers')
    runbot_containers_memory = fields.Float('Memory limit for containers (in GiB)')
    runbot_memory_bytes = fields.Float('Bytes', compute='_compute_memory_bytes')
    runbot_running_max = fields.Integer('Maximum number of running builds')
    runbot_timeout = fields.Integer('Max allowed step timeout (in seconds)')
    runbot_starting_port = fields.Integer('Starting port for running builds')
    runbot_max_age = fields.Integer('Max commit age (in days)')
    runbot_logdb_name = fields.Char('Local Logs DB name', default='runbot_logs', config_parameter='runbot.logdb_name')
    runbot_update_frequency = fields.Integer('Update frequency (in seconds)')
    runbot_template = fields.Char('Postgresql template', help="Postgresql template to use when creating DB's")
    runbot_message = fields.Text('Frontend warning message', help="Will be displayed on the frontend when not empty")
    runbot_default_odoorc = fields.Text('Default odoorc for builds')
    runbot_upgrade_exception_message = fields.Text('Upgrade exception message', help='Template to auto-generate a github message when creating an upgrade exception')
    runbot_do_fetch = fields.Boolean('Discover new commits')
    runbot_do_schedule = fields.Boolean('Schedule builds')
    runbot_is_base_regex = fields.Char('Regex is_base')
    runbot_forwardport_author = fields.Char('Forwardbot author')
    runbot_organisation = fields.Char('Organisation')
    runbot_disable_host_on_fetch_failure = fields.Boolean('Disable host on fetch failure')
    runbot_use_ssl = fields.Boolean('Use ssl for workers', help="select if worker ressources (log, dump, ...) uses ssl or not.", config_parameter="runbot.use_ssl")

    runbot_db_gc_days = fields.Integer(
        'Days before gc',
        default=30,
        config_parameter='runbot.db_gc_days',
        help="Time after the build finished (running time included) to wait before droping db and non log files")
    runbot_db_gc_days_child = fields.Integer(
        'Days before gc of child',
        default=15,
        config_parameter='runbot.db_gc_days_child',
        help='Children should have a lower gc delay since the   database usually comes from the parent or a multibuild')
    runbot_full_gc_days = fields.Integer(
        'Days before directory removal',
        default=365,
        config_parameter='runbot.full_gc_days',
        help='Number of days to wait after to first gc to completely remove build directory (remaining test/log files)')

    runbot_pending_warning = fields.Integer('Pending warning limit', default=5, config_parameter='runbot.pending.warning')
    runbot_pending_critical = fields.Integer('Pending critical limit', default=5, config_parameter='runbot.pending.critical')

    # TODO other icp
    # runbot.runbot_maxlogs 100
    # migration db
    # ln path

    @api.model
    def get_values(self):
        res = super(ResConfigSettings, self).get_values()
        get_param = self.env['ir.config_parameter'].sudo().get_param
        res.update(runbot_workers=int(get_param('runbot.runbot_workers', default=2)),
                   runbot_containers_memory=float(get_param('runbot.runbot_containers_memory', default=0)),
                   runbot_running_max=int(get_param('runbot.runbot_running_max', default=5)),
                   runbot_timeout=int(get_param('runbot.runbot_timeout', default=10000)),
                   runbot_starting_port=int(get_param('runbot.runbot_starting_port', default=2000)),
                   runbot_max_age=int(get_param('runbot.runbot_max_age', default=30)),
                   runbot_update_frequency=int(get_param('runbot.runbot_update_frequency', default=10)),
                   runbot_template=get_param('runbot.runbot_db_template'),
                   runbot_message=get_param('runbot.runbot_message', default=''),
                   runbot_default_odoorc=get_param('runbot.runbot_default_odoorc'),
                   runbot_upgrade_exception_message=get_param('runbot.runbot_upgrade_exception_message'),
                   runbot_do_fetch=get_param('runbot.runbot_do_fetch', default=False),
                   runbot_do_schedule=get_param('runbot.runbot_do_schedule', default=False),
                   runbot_is_base_regex=get_param('runbot.runbot_is_base_regex', default=''),
                   runbot_forwardport_author=get_param('runbot.runbot_forwardport_author', default=''),
                   runbot_organisation=get_param('runbot.runbot_organisation', default=''),
                   runbot_disable_host_on_fetch_failure=get_param('runbot.runbot_disable_host_on_fetch_failure', default=False),
                   )
        return res

    def set_values(self):
        super(ResConfigSettings, self).set_values()
        set_param = self.env['ir.config_parameter'].sudo().set_param
        set_param("runbot.runbot_workers", self.runbot_workers)
        set_param("runbot.runbot_containers_memory", self.runbot_containers_memory)
        set_param("runbot.runbot_running_max", self.runbot_running_max)
        set_param("runbot.runbot_timeout", self.runbot_timeout)
        set_param("runbot.runbot_starting_port", self.runbot_starting_port)
        set_param("runbot.runbot_max_age", self.runbot_max_age)
        set_param('runbot.runbot_update_frequency', self.runbot_update_frequency)
        set_param('runbot.runbot_db_template', self.runbot_template)
        set_param('runbot.runbot_message', self.runbot_message)
        set_param('runbot.runbot_default_odoorc', self.runbot_default_odoorc)
        set_param('runbot.runbot_upgrade_exception_message', self.runbot_upgrade_exception_message)
        set_param('runbot.runbot_do_fetch', self.runbot_do_fetch)
        set_param('runbot.runbot_do_schedule', self.runbot_do_schedule)
        set_param('runbot.runbot_is_base_regex', self.runbot_is_base_regex)
        set_param('runbot.runbot_forwardport_author', self.runbot_forwardport_author)
        set_param('runbot.runbot_organisation', self.runbot_organisation)
        set_param('runbot.runbot_disable_host_on_fetch_failure', self.runbot_disable_host_on_fetch_failure)

    @api.onchange('runbot_is_base_regex')
    def _on_change_is_base_regex(self):
        """ verify that the base_regex is valid
        """
        if self.runbot_is_base_regex:
            try:
                re.compile(self.runbot_is_base_regex)
            except re.error:
                raise UserError("The regex is invalid")

    @api.depends('runbot_containers_memory')
    def _compute_memory_bytes(self):
        for rec in self:
            if rec.runbot_containers_memory > 0:
                rec.runbot_memory_bytes = rec.runbot_containers_memory * 1024 ** 3
            else:
                rec.runbot_memory_bytes = 0
