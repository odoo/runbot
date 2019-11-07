# -*- coding: utf-8 -*-

from odoo import fields, models, api


class MultiBuildWizard(models.TransientModel):

    _name = 'runbot.build.config.multi.wizard'

    base_name = fields.Char('Generic name', required=True)
    prefix = fields.Char('Prefix', help="Leave blank to use login.")
    config_multi_name = fields.Char('Config name')
    step_create_multi_name = fields.Char('Create multi step name')
    config_single_name = fields.Char('Config only name')
    config_single_extra_params = fields.Char('Extra cmd args')
    config_single_test_tags = fields.Char('Test tags', default='')
    config_single_test_enable = fields.Boolean('Enable tests', default=True)
    step_single_name = fields.Char('Only step name')
    number_builds = fields.Integer('Number of multi builds', default=10)
    modules = fields.Char('Modules to install', help="List of module patterns to install, use * to install all available modules, prefix the pattern with dash to remove the module.", default='')

    @api.onchange('base_name', 'prefix')
    def _onchange_name(self):
        if self.base_name:
            prefix = self.env.user.login.split('@')[0] if not self.prefix else self.prefix
            self.prefix = prefix
            name = '%s %s' % (prefix, self.base_name.capitalize())
            step_name = name.replace(' ', '_').lower()

            self.config_multi_name = '%s Multi' % name
            self.step_create_multi_name = '%s_create_multi' % step_name
            self.config_single_name = '%s Single' % name
            self.step_single_name = '%s_single' % step_name

    def generate(self):
        if self.base_name:
            # Create the "only" step and config
            step_single = self.env['runbot.build.config.step'].create({
                'name': self.step_single_name,
                'job_type': 'install_odoo',
                'test_tags': self.config_single_test_tags,
                'extra_params': self.config_single_extra_params,
                'test_enable': self.config_single_test_enable,
                'install_modules': self.modules,
            })
            config_single = self.env['runbot.build.config'].create({'name': self.config_single_name})

            self.env['runbot.build.config.step.order'].create({
                'sequence': 10,
                'config_id': config_single.id,
                'step_id': step_single.id
            })

            # Create the multiple builds step and config
            step_create_multi = self.env['runbot.build.config.step'].create({
                'name': self.step_create_multi_name,
                'job_type': 'create_build',
                'create_config_ids': [(4, config_single.id)],
                'number_builds': self.number_builds,
                'hide_build': True,
                'force_build': True
            })

            config_multi = self.env['runbot.build.config'].create({'name': self.config_multi_name})

            config_multi.group = config_multi
            step_create_multi.group = config_multi
            config_single.group = config_multi
            step_single.group = config_multi

            self.env['runbot.build.config.step.order'].create({
                'sequence': 10,
                'config_id': config_multi.id,
                'step_id': step_create_multi.id
            })
