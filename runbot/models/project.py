from odoo import models, fields, api


class Project(models.Model):
    _name = 'runbot.project'
    _description = 'Project'
    _order = 'sequence, id'

    name = fields.Char('Project name', required=True)
    group_ids = fields.Many2many('res.groups', string='Required groups')
    keep_sticky_running = fields.Boolean('Keep last sticky builds running')
    trigger_ids = fields.One2many('runbot.trigger', 'project_id', string='Triggers')
    dockerfile_id = fields.Many2one('runbot.dockerfile', index=True, help="Project Default Dockerfile")
    repo_ids = fields.One2many('runbot.repo', 'project_id', string='Repos')
    sequence = fields.Integer('Sequence')
    organisation = fields.Char('organisation', default=lambda self: self.env['ir.config_parameter'].sudo().get_param('runbot.runbot_organisation'))
    token = fields.Char("Github token", groups="runbot.group_runbot_admin")
    master_bundle_id = fields.Many2one('runbot.bundle', string='Master bundle')
    dummy_bundle_id = fields.Many2one('runbot.bundle', string='Dummy bundle')

    @api.model_create_multi
    def create(self, create_values):
        projects = super().create(create_values)
        base_bundle_values = []
        dummy_bundle_values = []
        for project in projects:
            base_bundle_values.append({
                'project_id': project.id,
                'name': 'master',
                'is_base': True,
            })
            dummy_bundle_values.append({
                'project_id': project.id,
                'name': 'Dummy',
                'no_build': True,
            })
        master_bundles = self.env['runbot.bundle'].create(base_bundle_values)
        dummy_bundles = self.env['runbot.bundle'].create(dummy_bundle_values)
        for project, bundle in zip(projects, master_bundles):
            project.master_bundle_id = bundle
        for project, bundle in zip(projects, dummy_bundles):
            project.dummy_bundle_id = bundle
        return projects


class Category(models.Model):
    _name = 'runbot.category'
    _description = 'Trigger category'

    name = fields.Char("Name")
    icon = fields.Char("Font awesome icon")
    view_id = fields.Many2one('ir.ui.view', "Link template")
