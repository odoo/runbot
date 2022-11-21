# -*- coding: utf-8 -*-
import ast
import hashlib
import logging
import re

from collections import defaultdict
from dateutil.relativedelta import relativedelta
from fnmatch import fnmatch
from odoo import models, fields, api
from odoo.exceptions import ValidationError, UserError

_logger = logging.getLogger(__name__)


class RunbotTeam(models.Model):

    _name = 'runbot.team'
    _description = "Runbot Team"
    _order = 'name, id'

    name = fields.Char('Team', required=True)
    user_ids = fields.Many2many('res.users', string='Team Members', domain=[('share', '=', False)])
    dashboard_id = fields.Many2one('runbot.dashboard', string='Dashboard')
    build_error_ids = fields.One2many('runbot.build.error', 'team_id', string='Team Errors', domain=[('parent_id', '=', False)])
    path_glob = fields.Char('Module Wildcards',
        help='Comma separated list of `fnmatch` wildcards used to assign errors automaticaly\n'
        'Negative wildcards starting with a `-` can be used to discard some path\n'
        'e.g.: `*website*,-*website_sale*`')
    module_ownership_ids = fields.One2many('runbot.module.ownership', 'team_id')
    upgrade_exception_ids = fields.One2many('runbot.upgrade.exception', 'team_id', string='Team Upgrade Exceptions')
    github_team = fields.Char('Github team')

    @api.model_create_single
    def create(self, values):
        if 'dashboard_id' not in values or values['dashboard_id'] == False:
            dashboard = self.env['runbot.dashboard'].search([('name', '=', values['name'])])
            if not dashboard:
                dashboard = dashboard.create({'name': values['name']})
            values['dashboard_id'] = dashboard.id
        return super().create(values)

    @api.model
    def _get_team(self, module_name):
        for team in self.env['runbot.team'].search([('path_glob', '!=', False)]):
            if any([fnmatch(module_name, pattern.strip().strip('-')) for pattern in team.path_glob.split(',') if pattern.strip().startswith('-')]):
                continue
            if any([fnmatch(module_name, pattern.strip()) for pattern in team.path_glob.split(',') if not pattern.strip().startswith('-')]):
                return team.id
        return False

class Module(models.Model):
    _name = 'runbot.module'
    _description = 'Modules'

    name = fields.Char('Name')
    ownership_ids = fields.One2many('runbot.module.ownership', 'module_id')


class ModuleOwnership(models.Model):
    _name = 'runbot.module.ownership'
    _description = "Module ownership"

    module_id = fields.Many2one('runbot.module', string='Module', required=True, ondelete='cascade')
    team_id = fields.Many2one('runbot.team', string='Team', required=True)
    is_fallback = fields.Boolean('Fallback')


class RunbotDashboard(models.Model):

    _name = 'runbot.dashboard'
    _description = "Runbot Dashboard"
    _order = 'name, id'

    name = fields.Char('Team', required=True)
    team_ids = fields.One2many('runbot.team', 'dashboard_id', string='Teams')
    dashboard_tile_ids = fields.Many2many('runbot.dashboard.tile', string='Dashboards tiles')


class RunbotDashboardTile(models.Model):

    _name = 'runbot.dashboard.tile'
    _description = "Runbot Dashboard Tile"
    _order = 'sequence, id'

    sequence = fields.Integer('Sequence')
    name = fields.Char('Name')
    dashboard_ids = fields.Many2many('runbot.dashboard', string='Dashboards')
    display_name = fields.Char(compute='_compute_display_name')
    project_id = fields.Many2one('runbot.project', 'Project', help='Project to monitor', required=True,
        default=lambda self: self.env.ref('runbot.main_project'))
    category_id = fields.Many2one('runbot.category', 'Category', help='Trigger Category to monitor', required=True,
        default=lambda self: self.env.ref('runbot.default_category'))
    trigger_id = fields.Many2one('runbot.trigger', 'Trigger', help='Trigger to monitor in chosen category')
    config_id = fields.Many2one('runbot.build.config', 'Config', help='Select a sub_build with this config')
    domain_filter = fields.Char('Domain Filter', help='If present, will be applied on builds', default="[('global_result', '=', 'ko')]")
    custom_template_id = fields.Many2one('ir.ui.view', help='Change for a custom Dashboard card template',
        domain=[('type', '=', 'qweb')], default=lambda self: self.env.ref('runbot.default_dashboard_tile_view'))
    sticky_bundle_ids = fields.Many2many('runbot.bundle', compute='_compute_sticky_bundle_ids', string='Sticky Bundles')
    build_ids = fields.Many2many('runbot.build', compute='_compute_build_ids', string='Builds')

    @api.depends('project_id', 'category_id', 'trigger_id', 'config_id')
    def _compute_display_name(self):
        for board in self:
            names = [board.project_id.name, board.category_id.name, board.trigger_id.name, board.config_id.name, board.name]
            board.display_name = ' / '.join([n for n in names if n])

    @api.depends('project_id')
    def _compute_sticky_bundle_ids(self):
        sticky_bundles = self.env['runbot.bundle'].search([('sticky', '=', True)])
        for dashboard in self:
            dashboard.sticky_bundle_ids = sticky_bundles.filtered(lambda b: b.project_id == dashboard.project_id)

    @api.depends('project_id', 'category_id', 'trigger_id', 'config_id', 'domain_filter')
    def _compute_build_ids(self):
        for dashboard in self:
            last_done_batch_ids = dashboard.sticky_bundle_ids.with_context(category_id=dashboard.category_id.id).last_done_batch
            if dashboard.trigger_id:
                all_build_ids = last_done_batch_ids.slot_ids.filtered(lambda s: s.trigger_id == dashboard.trigger_id).all_build_ids
            else:
                all_build_ids = last_done_batch_ids.all_build_ids

            domain = ast.literal_eval(dashboard.domain_filter) if dashboard.domain_filter else []
            if dashboard.config_id:
                domain.append(('config_id', '=', dashboard.config_id.id))
            dashboard.build_ids = all_build_ids.filtered_domain(domain)
