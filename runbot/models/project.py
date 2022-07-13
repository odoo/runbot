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
    always_use_foreign = fields.Boolean('Use foreign bundle', help='By default, check for the same bundle name in another project to fill missing commits.', default=False)
    tmp_prefix = fields.Char('tmp branches prefix', default="tmp.")
    staging_prefix = fields.Char('staging branches prefix', default="staging.")
    hidden = fields.Boolean('Hidden', help='Hide this project from the main page')
    active = fields.Boolean("Active", default=True)

    @api.model_create_multi
    def create(self, vals_list):
        projects = super().create(vals_list)
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

    def _get_description(self):
        return[
            {
                'id': r.id,
                'url': f'{r.get_base_url()}/runbot/json/projects/{r.id}',
                'name': r.name,
                'keep_sticky_running': r.keep_sticky_running,
                'bundles_url': f'{r.get_base_url()}/runbot/json/projects/{r.id}/bundles'
            }
            for r in self
        ]

class Category(models.Model):
    _name = 'runbot.category'
    _description = 'Trigger category'

    name = fields.Char("Name")
    icon = fields.Char("Font awesome icon")
    view_id = fields.Many2one('ir.ui.view', "Link template")
