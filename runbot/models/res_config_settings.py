# -*- coding: utf-8 -*-

from .. import common
from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    runbot_workers = fields.Integer('Total number of workers')
    runbot_running_max = fields.Integer('Maximum number of running builds')
    runbot_timeout = fields.Integer('Default timeout (in seconds)')
    runbot_starting_port = fields.Integer('Starting port for running builds')
    runbot_domain = fields.Char('Runbot domain')
    runbot_max_age = fields.Integer('Max branch age (in days)')

    @api.model
    def get_values(self):
        res = super(ResConfigSettings, self).get_values()
        get_param = self.env['ir.config_parameter'].sudo().get_param
        res.update(runbot_workers=int(get_param('runbot.runbot_workers', default=6)),
                   runbot_running_max=int(get_param('runbot.runbot_running_max', default=75)),
                   runbot_timeout=int(get_param('runbot.runbot_timeout', default=1800)),
                   runbot_starting_port=int(get_param('runbot.runbot_starting_port', default=2000)),
                   runbot_domain=get_param('runbot.runbot_domain', default=common.fqdn()),
                   runbot_max_age=int(get_param('runbot.runbot_max_age', default=30)))
        return res

    @api.multi
    def set_values(self):
        super(ResConfigSettings, self).set_values()
        set_param = self.env['ir.config_parameter'].sudo().set_param
        set_param("runbot.runbot_workers", self.runbot_workers)
        set_param("runbot.runbot_running_max", self.runbot_running_max)
        set_param("runbot.runbot_timeout", self.runbot_timeout)
        set_param("runbot.runbot_starting_port", self.runbot_starting_port)
        set_param("runbot.runbot_domain", self.runbot_domain)
        set_param("runbot.runbot_max_age", self.runbot_max_age)
