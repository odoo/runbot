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

    # minimal config options
    bundle_id = fields.Many2one('runbot.bundle', "Bundle")
    project_id = fields.Many2one(related='bundle_id.project_id', string='Project')
    trigger_id = fields.Many2one('runbot.trigger', domain="[('project_id', '=', project_id)]")
    config_id = fields.Many2one('runbot.build.config', string="Config id", default=lambda self: self.env.ref('runbot.runbot_build_config_custom_multi'))

    # base options
    config_data = JsonDictField("Config data")
    extra_params = fields.Char('Extra params', default='')

    # restore options
    restore_mode = fields.Selection([('auto', 'Auto'), ('url', 'Dump url')])
    restore_dump_url = fields.Char('Dump url for children')
    restore_trigger_id = fields.Many2one('runbot.trigger', 'Trigger to restore a dump', domain="[('project_id', '=', project_id), ('manual', '=', False)]")
    restore_database_suffix = fields.Char('Database suffix to restore', default='all')

    # create multi options
    number_build = fields.Integer('Number builds for config multi', default=10)
    child_extra_params = fields.Char('Extra params for children', default='--test-tags /module.test_method')
    child_config_id = fields.Many2one('runbot.build.config', 'Config for children', default=lambda self: self.env.ref('runbot.runbot_build_config_restore_and_test'))

    warnings = fields.Text('Warnings', readonly=True)

    has_create_step = fields.Boolean("Hase create step", compute="_compute_has_create_step")
    has_restore_step = fields.Boolean("Hase restore step", compute="_compute_has_restore_step")
    has_child_with_restore_step = fields.Boolean("Child config has create step", compute="_compute_has_child_with_restore_step")

    @api.depends('config_id')
    def _compute_has_create_step(self):
        for record in self:
            record.has_create_step = any(step.job_type == 'create_build' for step in self.config_id.step_ids)

    @api.depends('config_id')
    def _compute_has_restore_step(self):
        for record in self:
            record.has_restore_step = any(step.job_type == 'restore' for step in self.config_id.step_ids)

    @api.depends('child_config_id')
    def _compute_has_child_with_restore_step(self):
        for record in self:
            record.has_child_with_restore_step = record.child_config_id and any(step.job_type == 'restore' for step in self.child_config_id.step_ids)

    @api.onchange('extra_params', 'child_extra_params', 'restore_dump_url', 'config_id', 'child_config_id', 'number_build', 'config_id', 'restore_mode', 'restore_database_suffix', 'restore_trigger_id')
    def _onchange_warnings(self):
        for wizard in self:
            _warnings = []

            if not wizard.trigger_id:
                _warnings.append(f'No trigger id given (required and may automatically fix other issues)')

            if wizard._get_existing_trigger():
                _warnings.append(f'A custom trigger already exists for trigger {wizard.trigger_id.name} and will be unlinked')

            if wizard.restore_mode:
                if (not wizard.has_restore_step and not wizard.has_child_with_restore_step):
                    _warnings.append('A restore mode is defined but no config has a restore step')
            elif not wizard.restore_mode:
                if wizard.has_restore_step :
                    _warnings.append('Config has a restore step but no restore mode is given')
                if wizard.has_child_with_restore_step:
                    _warnings.append('Child config has a restore step but no restore mode is given')
            elif wizard.restore_mode == "url":
                if not wizard.restore_dump_url:
                    _warnings.append('The restore mode is url but no dump_url is given')
            elif wizard.restore_mode == "auto":
                if not wizard.restore_trigger_id:
                    _warnings.append('The restore mode is auto but no restore trigger is given')
                if not wizard.restore_database_suffix:
                    _warnings.append('The restore mode is auto but no db suffix is given')

            if wizard.has_create_step:
                if not wizard.child_config_id:
                    _warnings.append('Config has a create step nut no child config given')
                if not wizard.child_extra_params:
                    _warnings.append('Config has a create step nut no child extra param given')
                    if wizard.extra_params:
                        _warnings.append('You may change `Extra params` to `Extra params for children`')
            else:
                if wizard.child_extra_params:
                    _warnings.append('Extra params for children given but config has no create step')
                if wizard.child_config_id:
                    _warnings.append('Config for children given but config has no create step')
                if not wizard.extra_params:
                    _warnings.append('No extra params are given')

            if not wizard.trigger_id.manual:
                _warnings.append("This custom trigger will replace an existing non manual trigger. The ci won't be sent anymore")

            wizard.warnings = '\n'.join(_warnings)

    @api.onchange('trigger_id')
    def _onchange_trigger_id(self):
        for wizard in self:
            if wizard.trigger_id:
                wizard.restore_trigger_id = wizard.trigger_id.restore_trigger_id
                if wizard.restore_trigger_id and not wizard.restore_mode:
                    wizard.restore_mode = 'auto'
        self._onchange_config_data()
        self._onchange_warnings()

    @api.onchange('number_build', 'extra_params', 'child_extra_params', 'restore_dump_url', 'child_config_id', 'restore_trigger_id', 'restore_database_suffix', 'restore_mode')
    def _onchange_config_data(self):
       for wizard in self:
           wizard.config_data = self._get_config_data()

    def _get_config_data(self):
        config_data = {}
        if self.number_build:
            config_data['number_build'] = self.number_build
        if self.extra_params:
            config_data['extra_params'] = self.extra_params
        child_data = {}
        if self.child_extra_params:
            child_data['extra_params'] = self.child_extra_params
        if self.restore_mode:
            restore_params = {}
            if self.restore_mode == 'url':
                if self.restore_dump_url:
                    restore_params['dump_url'] = self.restore_dump_url
            else:
                if self.restore_trigger_id:
                    restore_params['dump_trigger_id'] = self.restore_trigger_id.id
                if self.restore_database_suffix:
                    restore_params['dump_suffix'] = self.restore_database_suffix
            if self.has_child_with_restore_step:
                child_data['config_data'] = restore_params
            if not self.has_child_with_restore_step or self.has_restore_step:
                config_data.update(restore_params)

        if self.child_config_id:
            child_data['config_id'] = self.child_config_id.id
        if child_data:
            config_data['child_data'] = child_data
        return config_data

    def _get_existing_trigger(self):
        return self.env['runbot.bundle.trigger.custom'].search([('bundle_id', '=', self.bundle_id.id), ('trigger_id', '=', self.trigger_id.id)])

    def action_submit(self):
        self.ensure_one()
        self._get_existing_trigger().unlink()
        self.env['runbot.bundle.trigger.custom'].create({
            'bundle_id': self.bundle_id.id,
            'trigger_id': self.trigger_id.id,
            'config_id': self.config_id.id,
            'config_data': self.config_data,
        })
