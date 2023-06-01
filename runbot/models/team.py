# -*- coding: utf-8 -*-
import ast
import hashlib
import logging
import re

from ..common import _make_github_session
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
    _inherit = 'mail.thread'

    name = fields.Char('Team', required=True)
    project_id = fields.Many2one('runbot.project', 'Project', help='Project to monitor', required=True,
                                 default=lambda self: self.env.ref('runbot.main_project'))
    organisation = fields.Char('organisation', related="project_id.organisation")
    user_ids = fields.Many2many('res.users', string='Team Members', domain=[('share', '=', False)])
    dashboard_id = fields.Many2one('runbot.dashboard', string='Dashboard')
    build_error_ids = fields.One2many('runbot.build.error', 'team_id', string='Team Errors', domain=[('parent_id', '=', False)])
    path_glob = fields.Char(
        'Module Wildcards',
        help='Comma separated list of `fnmatch` wildcards used to assign errors automaticaly\n'
        'Negative wildcards starting with a `-` can be used to discard some path\n'
        'e.g.: `*website*,-*website_sale*`'
    )
    module_ownership_ids = fields.One2many('runbot.module.ownership', 'team_id')
    codeowner_ids = fields.One2many('runbot.codeowner', 'team_id')
    trigger_ids = fields.Many2many('runbot.trigger', string='Followed triggers')
    upgrade_exception_ids = fields.One2many('runbot.upgrade.exception', 'team_id', string='Team Upgrade Exceptions')
    github_team = fields.Char('Github team', tracking=True)
    github_logins = fields.Char('Github logins', help='Additional github logins, prefer adding the login on the member of the team', tracking=True)
    skip_team_pr = fields.Boolean('Skip team pr', help="Don't add codeowner if pr was created by a member of the team", tracking=True)
    skip_fw_pr = fields.Boolean('Skip forward-port pr', help="Don't add codeowner if pr is a forwardport, even when forced pushed", tracking=True)

    @api.model_create_single
    def create(self, values):
        if 'dashboard_id' not in values or values['dashboard_id'] == False:
            dashboard = self.env['runbot.dashboard'].search([('name', '=', values['name'])])
            if not dashboard:
                dashboard = dashboard.create({'name': values['name']})
            values['dashboard_id'] = dashboard.id
        return super().create(values)

    @api.model
    def _get_team(self, file_path, repos=None):
        # path = file_path.removeprefix('/data/build/')
        path = file_path
        if path.startswith('/data/build/'):
            path = path.split('/', 3)[3]

        repo_name = path.split('/')[0]
        module = None
        if repos:
            repos = repos.filtered(lambda repo: repo.name == repo_name)
        else:
            repos = self.env['runbot.repo'].search([('name', '=', repo_name)])
        for repo in repos:
            module = repo._get_module(path)
            if module:
                break
        if module:
            for ownership in self.module_ownership_ids.sorted(lambda t: t.is_fallback):
                if module == ownership.module_id.name:
                    return ownership.team_id

        for team in self:
            if not team.path_glob:
                continue
            if any([fnmatch(file_path, pattern.strip().strip('-')) for pattern in team.path_glob.split(',') if pattern.strip().startswith('-')]):
                continue
            if any([fnmatch(file_path, pattern.strip()) for pattern in team.path_glob.split(',') if not pattern.strip().startswith('-')]):
                return team
        return False

    def _get_members_logins(self):
        self.ensure_one()
        team_loggins = set()
        if self.github_logins:
            team_loggins = set(self.github_logins.split(','))
        team_loggins |= set(self.user_ids.filtered(lambda user: user.github_login).mapped('github_login'))
        return team_loggins

    def _fetch_members(self):
        self.check_access_rights('write')
        self.check_access_rule('write')
        for team in self:
            if team.github_team:
                url = f"https://api.github.com/orgs/{team.organisation}/teams/{team.github_team}"
                session = _make_github_session(team.project_id.sudo().token)
                response = session.get(url)
                if response.status_code != 200:
                    raise UserError(f'Cannot find team {team.github_team}')
                team_infos = response.json()
                members_url = team_infos['members_url'].replace('{/member}', '')
                members = session.get(members_url).json()
                team_members_logins = set(team.user_ids.mapped('github_login'))
                members = [member['login'] for member in members if member['login'] not in team_members_logins]
                team.github_logins = ','.join(sorted(members))


class Module(models.Model):
    _name = 'runbot.module'
    _description = 'Modules'

    name = fields.Char('Name')
    ownership_ids = fields.One2many('runbot.module.ownership', 'module_id')
    team_ids = fields.Many2many('runbot.team', string="Teams", compute='_compute_team_ids')

    @api.depends('ownership_ids')
    def _compute_team_ids(self):
        for record in self:
            record.team_ids = record.ownership_ids.team_id


class ModuleOwnership(models.Model):
    _name = 'runbot.module.ownership'
    _description = "Module ownership"

    module_id = fields.Many2one('runbot.module', string='Module', required=True, ondelete='cascade')
    team_id = fields.Many2one('runbot.team', string='Team', required=True)
    is_fallback = fields.Boolean('Fallback')

    def name_get(self):
        return [(record.id, f'{record.module_id.name} -> {record.team_id.name}{" (fallback)" if record.is_fallback else ""}' ) for record in self]


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
