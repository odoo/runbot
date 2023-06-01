import json

from odoo import models, fields, api
from ..fields import JsonDictField

class BundleTriggerCustomization(models.Model):
    _name = 'runbot.bundle.trigger.custom'
    _description = 'Custom trigger'

    trigger_id = fields.Many2one('runbot.trigger')
    start_mode = fields.Selection([('disabled', 'Disabled'), ('auto', 'Auto'), ('force', 'Force')], required=True, default='auto')
    bundle_id = fields.Many2one('runbot.bundle')
    config_id = fields.Many2one('runbot.build.config')
    extra_params = fields.Char("Custom parameters")
    config_data = JsonDictField("Config data")

    _sql_constraints = [
        (
            "bundle_custom_trigger_unique",
            "unique (bundle_id, trigger_id)",
            "Only one custom trigger per trigger per bundle is allowed",
        )
    ]

class CustomTriggerWizard(models.TransientModel):
    _name = 'runbot.trigger.custom.wizard'
    _description = 'Custom trigger Wizard'

    bundle_id = fields.Many2one('runbot.bundle', "Bundle")
    project_id = fields.Many2one(related='bundle_id.project_id', string='Project')
    trigger_id = fields.Many2one('runbot.trigger', domain="[('project_id', '=', project_id)]")
    config_id = fields.Many2one('runbot.build.config', string="Config id", default=lambda self: self.env.ref('runbot.runbot_build_config_custom_multi'))

    config_data = JsonDictField("Config data")

    number_build = fields.Integer('Number builds for config multi', default=10)

    child_extra_params = fields.Char('Extra params for children', default='--test-tags /module.test_method')
    child_dump_url = fields.Char('Dump url for children')
    child_config_id = fields.Many2one('runbot.build.config', 'Config for children', default=lambda self: self.env.ref('runbot.runbot_build_config_restore_and_test'))

    warnings = fields.Text('Warnings', readonly=True)

    @api.onchange('child_extra_params', 'child_dump_url', 'child_config_id', 'number_build', 'config_id', 'trigger_id')
    def _onchange_warnings(self):
        for wizard in self:
            _warnings = []
            if wizard._get_existing_trigger():
                _warnings.append(f'A custom trigger already exists for trigger {wizard.trigger_id.name} and will be unlinked')

            if wizard.child_dump_url or wizard.child_extra_params or wizard.child_config_id or wizard.number_build:
                if not any(step.job_type == 'create_build' for step in wizard.config_id.step_ids()):
                    _warnings.append('Some multi builds params are given but config as no create step')

            if wizard.child_dump_url and not any(step.job_type == 'restore' for step in wizard.child_config_id.step_ids()):
                _warnings.append('A dump_url is defined but child config has no restore step')
        
            if not wizard.child_dump_url and any(step.job_type == 'restore' for step in wizard.child_config_id.step_ids()):
                _warnings.append('Child config has a restore step but no dump_url is given')

            if not wizard.trigger_id.manual:
                _warnings.append("This custom trigger will replace an existing non manual trigger. The ci won't be sent anymore")

            wizard.warnings = '\n'.join(_warnings)

    @api.onchange('number_build', 'child_extra_params', 'child_dump_url', 'child_config_id')
    def _onchange_config_data(self):
        for wizard in self:
            wizard.config_data = self._get_config_data()

    def _get_config_data(self):
        config_data = {}
        if self.number_build:
            config_data['number_build'] = self.number_build
        child_data = {}
        if self.child_extra_params:
            child_data['extra_params'] = self.child_extra_params
        if self.child_dump_url:
            child_data['config_data'] = {'dump_url': self.child_dump_url}
        if self.child_config_id:
            child_data['config_id'] = self.child_config_id.id
        if child_data:
            config_data['child_data'] = child_data
        return config_data

    def _get_existing_trigger(self):
        return self.env['runbot.bundle.trigger.custom'].search([('bundle_id', '=', self.bundle_id.id), ('trigger_id', '=', self.trigger_id.id)])

    def submit(self):
        self.ensure_one()
        self._get_existing_trigger().unlink()
        self.env['runbot.bundle.trigger.custom'].create({
            'bundle_id': self.bundle_id.id,
            'trigger_id': self.trigger_id.id,
            'config_id': self.config_id.id,
            'config_data': self.config_data,
        })
