# -*- coding: utf-8 -*-
import re

from .. import common
from odoo import api, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    runbot_workers = fields.Integer('Default number of workers')
    runbot_containers_memory = fields.Float('Containers Mem limit (in GiB)')
    runbot_containers_cpus = fields.Float('Allowed Containers CPUs (0 means no limit)')
    runbot_memory_bytes = fields.Float('Bytes', compute='_compute_memory_bytes')
    runbot_running_max = fields.Integer('Max running builds')
    runbot_timeout = fields.Integer('Max step timeout (in seconds)')
    runbot_starting_port = fields.Integer('Starting port for running builds')
    runbot_max_age = fields.Integer('Max commit age (in days)')
    runbot_logdb_name = fields.Char('Local Logs DB name', default='runbot_logs', config_parameter='runbot.logdb_name')
    runbot_template = fields.Char('Postgresql template', help="Postgresql template to use when creating DB's")
    runbot_message = fields.Text('Frontend warning message', help="Will be displayed on the frontend when not empty")
    runbot_default_odoorc = fields.Text('Default odoorc for builds')
    runbot_upgrade_exception_message = fields.Text('Upgrade exception message', help='Template to auto-generate a github message when creating an upgrade exception')
    runbot_is_base_regex = fields.Char('Regex is_base')
    runbot_forwardport_author = fields.Char('Forwardbot author')
    runbot_organisation = fields.Char('Organisation')
    runbot_dockerfile_public_by_default = fields.Boolean('Docker files are public by default')
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

    runbot_docker_registry_host_id = fields.Many2one('runbot.host', 'Docker builder', help='Runbot host which handles Docker builds.', config_parameter='runbot.docker_registry_host_id')
    runbot_docker_registry_url = fields.Char('Docker Registry url', help='Remote Registry Url', config_parameter='runbot.docker_registry_url')
    # TODO other icp
    # runbot.runbot_maxlogs 100
    # migration db
    # ln path

    @api.model
    def get_values(self):
        res = super(ResConfigSettings, self).get_values()
        icp = self.env['ir.config_parameter'].sudo()
        res.update(runbot_workers=icp.get_int('runbot.runbot_workers', 2),
                   runbot_containers_cpus=icp.get_float('runbot.runbot_containers_cpus'),
                   runbot_containers_memory=icp.get_float('runbot.runbot_containers_memory'),
                   runbot_running_max=icp.get_int('runbot.runbot_running_max', 5),
                   runbot_timeout=icp.get_int('runbot.runbot_timeout', 10000),
                   runbot_starting_port=icp.get_int('runbot.runbot_starting_port', 2000),
                   runbot_max_age=icp.get_int('runbot.runbot_max_age', 30),
                   runbot_template=icp.get_str('runbot.runbot_db_template'),
                   runbot_message=icp.get_str('runbot.runbot_message'),
                   runbot_default_odoorc=icp.get_str('runbot.runbot_default_odoorc'),
                   runbot_upgrade_exception_message=icp.get_str('runbot.runbot_upgrade_exception_message'),
                   runbot_is_base_regex=icp.get_str('runbot.runbot_is_base_regex'),
                   runbot_forwardport_author=icp.get_str('runbot.runbot_forwardport_author'),
                   runbot_organisation=icp.get_str('runbot.runbot_organisation'),
                   runbot_dockerfile_public_by_default=icp.get_bool('runbot.runbot_dockerfile_public_by_default'),
                   )
        return res

    def set_values(self):
        super(ResConfigSettings, self).set_values()
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_int("runbot.runbot_workers", self.runbot_workers)
        icp.set_float("runbot.runbot_containers_cpus", self.runbot_containers_cpus)
        icp.set_float("runbot.runbot_containers_memory", self.runbot_containers_memory)
        icp.set_int("runbot.runbot_running_max", self.runbot_running_max)
        icp.set_int("runbot.runbot_timeout", self.runbot_timeout)
        icp.set_int("runbot.runbot_starting_port", self.runbot_starting_port)
        icp.set_int("runbot.runbot_max_age", self.runbot_max_age)
        icp.set_str('runbot.runbot_db_template', self.runbot_template)
        icp.set_str('runbot.runbot_message', self.runbot_message)
        icp.set_str('runbot.runbot_default_odoorc', self.runbot_default_odoorc)
        icp.set_str('runbot.runbot_upgrade_exception_message', self.runbot_upgrade_exception_message)
        icp.set_str('runbot.runbot_is_base_regex', self.runbot_is_base_regex)
        icp.set_str('runbot.runbot_forwardport_author', self.runbot_forwardport_author)
        icp.set_str('runbot.runbot_organisation', self.runbot_organisation)
        icp.set_bool('runbot.runbot_dockerfile_public_by_default', self.runbot_dockerfile_public_by_default)

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
