from odoo import models, fields


class Project(models.Model):
    _name = 'runbot.project'
    _description = 'Project'

    name = fields.Char('Project name', required=True)
    group_ids = fields.Many2many('res.groups', string='Required groups')

    trigger_ids = fields.One2many('runbot.trigger', 'project_id', string='Triggers')


class Category(models.Model):
    _name = 'runbot.category'
    _description = 'Trigger category'

    name = fields.Char("Name")
    icon = fields.Char("Font awesome icon")
    view_id = fields.Many2one('ir.ui.view', "Link template")
