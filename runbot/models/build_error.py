# -*- coding: utf-8 -*-
import datetime
import hashlib
import json
import logging
import re

from collections import defaultdict
from dateutil.relativedelta import relativedelta
from dateutil import rrule
from markupsafe import Markup
from werkzeug.urls import url_join
from odoo import models, fields, api
from odoo.exceptions import ValidationError, UserError
from odoo.tools import SQL, lazy

from ..fields import JsonDictField

_logger = logging.getLogger(__name__)


def get_color(value: int):
    if value >= 10:
        return 'red'
    elif value >= 5:
        return 'orange'
    return 'green'

def draw_svg(values: list[int], max_value: int = 10, height: int = 30):
    lines = ''.join(f'<line x1="0" x2="{len(values) * 10}" y1="{v * 10}" y2="{v * 10}" stroke="gray" stroke_width="1"/>' for v in range(0, max_value, 2))
    rects = ''.join(f'<rect fill="{get_color(value)}" width="9" height="{min(value, max_value) * 10}" x="{idx * 10 + 0.5}" y="{(max_value - min(value, max_value)) * 10}"/>' for idx, value in enumerate(values))
    return f'<div style="height: {height}px"><svg xmlns="https://www.w3.org/2000/svg" viewbox="0 0 {len(values) * 10} {max_value * 10}" style="border: 1px solid black; height: 100%; width: 100%;" preserveAspectRatio="none" shape-rendering="cripsEdges">{lines}{rects}</svg></div>'

class BuildErrorLink(models.Model):
    _name = 'runbot.build.error.link'
    _description = 'Build Build Error Extended Relation'
    _order = 'log_date desc, build_id desc'

    build_id = fields.Many2one('runbot.build', required=True, index=True)
    error_content_id = fields.Many2one('runbot.build.error.content', required=True, index=True, ondelete='cascade')
    log_date = fields.Datetime(string='Log date')
    host = fields.Char(related='build_id.host')
    dest = fields.Char(related='build_id.dest')
    version_id = fields.Many2one(related='build_id.version_id')
    trigger_id = fields.Many2one(related='build_id.trigger_id')
    description = fields.Char(related='build_id.description')
    build_url = fields.Char(related='build_id.build_url')

    _sql_constraints = [
        ('error_build_rel_unique', 'UNIQUE (build_id, error_content_id)', 'A link between a build and an error must be unique'),
    ]


class BuildErrorSeenMixin(models.AbstractModel):
    _name = 'runbot.build.error.seen.mixin'
    _description = "Add last/firt build/log_date for error and asssignments"

    first_seen_build_id = fields.Many2one('runbot.build', compute='_compute_seen', string='First Seen build', store=True)
    first_seen_date = fields.Datetime(string='First Seen Date', compute='_compute_seen', store=True)
    last_seen_build_id = fields.Many2one('runbot.build', compute='_compute_seen', string='Last Seen build', store=True)
    last_seen_date = fields.Datetime(string='Last Seen Date', compute='_compute_seen', store=True)
    build_count = fields.Integer(string='Nb Seen', compute='_compute_seen', store=True)

    @api.depends('build_error_link_ids')
    def _compute_seen(self):
        for record in self:
            record.first_seen_date = False
            record.last_seen_date = False
            record.build_count = 0
            error_link_ids = record.build_error_link_ids.sorted('log_date')
            if error_link_ids:
                first_error_link = error_link_ids[0]
                last_error_link = error_link_ids[-1]
                record.first_seen_date = first_error_link.log_date
                record.last_seen_date = last_error_link.log_date
                record.first_seen_build_id = first_error_link.build_id
                record.last_seen_build_id = last_error_link.build_id
                record.build_count = len(error_link_ids.build_id)


def _compute_related_error_content_ids(field_name):
    @api.depends(f'error_content_ids.{field_name}')
    def _compute(self):
        for record in self:
            record[field_name] = record.error_content_ids[field_name]
    return _compute

def _search_related_error_content_ids(field_name):
    def _search(self, operator, value):
        return [(f'error_content_ids.{field_name}', operator, value)]
    return _search

class BuildError(models.Model):
    _name = "runbot.build.error"
    _description = "Build error"
    # An object to manage a group of errors log that fit together and assign them to a team
    _inherit = ('mail.thread', 'mail.activity.mixin', 'runbot.build.error.seen.mixin')


    name = fields.Char("Name")
    active = fields.Boolean('Open (not fixed)', default=True, tracking=True)
    description = fields.Text("Description", store=True, compute='_compute_description')
    content = fields.Text("Error contents", compute='_compute_content', search="_search_content")
    error_content_ids = fields.One2many('runbot.build.error.content', 'error_id')
    error_count = fields.Integer("Error count", store=True, compute='_compute_count')
    previous_error_id = fields.Many2one('runbot.build.error', string="Already seen error")

    responsible = fields.Many2one('res.users', 'Assigned fixer', tracking=True)
    customer = fields.Many2one('res.users', 'Customer', tracking=True)
    team_id = fields.Many2one('runbot.team', 'Assigned team', compute='_compute_team_id', inverse='_inverse_team_id', store=True, tracking=True)
    manual_team_id = fields.Many2one('runbot.team', 'Manually assigned team')
    auto_team_id = fields.Many2one('runbot.team', 'Automatically assigned team', readonly=True) # This is a computed field but not really
    fixing_commit = fields.Char('Fixing commit', tracking=True)
    fixing_pr_id = fields.Many2one('runbot.branch', 'Fixing PR', tracking=True, domain=[('is_pr', '=', True)])
    fixing_pr_alive = fields.Boolean('Fixing PR alive', related='fixing_pr_id.alive')
    fixing_pr_url = fields.Char('Fixing PR url', related='fixing_pr_id.branch_url')

    test_tags = fields.Char(string='Test tags', help="Comma separated list of test_tags to use to reproduce/remove this error", tracking=True)
    canonical_tags = fields.Char('Canonical tag', compute='_compute_canonical_tags', store=True)
    tags_min_version_excluded_id = fields.Many2one('runbot.version', 'Tag min version (excluded)')
    tags_min_version_id = fields.Many2one('runbot.version', 'Tags Min version', compute="_compute_tags_min_version_id", inverse="_inverse_tags_min_version_id", help="Minimal version where the test tags will be applied.", tracking=True)
    tags_max_version_id = fields.Many2one('runbot.version', 'Tags Max version', help="Maximal version where the test tags will be applied.", tracking=True)

    common_qualifiers = JsonDictField('Common Qualifiers', compute='_compute_common_qualifiers', store=True, help="Minimal qualifiers in common needed to link error content.")
    similar_ids = fields.One2many('runbot.build.error', compute='_compute_similar_ids', string="Similar Errors", help="Similar Errors based on common qualifiers")
    similar_content_ids = fields.One2many('runbot.build.error.content', compute='_compute_similar_content_ids', string="Similar Error Contents", help="Similar Error contents based on common qualifiers")
    unique_qualifiers = JsonDictField('Non conflicting Qualifiers', compute='_compute_unique_qualifiers', store=True, help="Non conflicting qualifiers in common needed to link error content.")
    analogous_ids = fields.One2many('runbot.build.error', compute='_compute_analogous_ids', string="Analogous Errors", help="Analogous Errors based on unique qualifiers")
    analogous_content_ids= fields.One2many('runbot.build.error.content', compute='_compute_analogous_content_ids', string="Analogous Error Contents", help="Analogous Error contents based on unique qualifiers")

    # Build error related data
    build_error_link_ids = fields.Many2many('runbot.build.error.link', compute=_compute_related_error_content_ids('build_error_link_ids'), search=_search_related_error_content_ids('build_error_link_ids'))
    unique_build_error_link_ids = fields.Many2many('runbot.build.error.link', compute='_compute_unique_build_error_link_ids')
    build_ids = fields.Many2many('runbot.build', compute=_compute_related_error_content_ids('build_ids'), search=_search_related_error_content_ids('build_ids'))
    bundle_ids = fields.Many2many('runbot.bundle', compute=_compute_related_error_content_ids('bundle_ids'), search=_search_related_error_content_ids('bundle_ids'))
    version_ids = fields.Many2many('runbot.version', string='Versions', compute=_compute_related_error_content_ids('version_ids'), search=_search_related_error_content_ids('version_ids'))
    trigger_ids = fields.Many2many('runbot.trigger', string='Triggers', compute=_compute_related_error_content_ids('trigger_ids'), store=True)
    tag_ids = fields.Many2many('runbot.build.error.tag', string='Tags', compute=_compute_related_error_content_ids('tag_ids'), search=_search_related_error_content_ids('tag_ids'))

    random = fields.Boolean('Random', compute="_compute_random", store=True)

    graph_history = fields.Html('30 days history', compute='_compute_graph', sanitize=False)
    graph_hourly_recurence = fields.Html('Hourly recurence', compute='_compute_graph', sanitize=False)
    graph_day_of_week_recurence = fields.Html('Weekly recurence', compute='_compute_graph', sanitize=False)
    graph_day_of_month_recurence = fields.Html('Monthly recurence', compute='_compute_graph', sanitize=False)


    @api.constrains('tags_min_version_id', 'tags_max_version_id')
    def _check_min_max_version(self):
        for build_error in self:
            if build_error.tags_min_version_id and build_error.tags_max_version_id and build_error.tags_min_version_id.number > build_error.tags_max_version_id.number:
                raise ValidationError('Tags Min version should be lower than Tags Max version')

    def _inverse_tags_min_version_id(self):
        all_versions = self.env['runbot.version'].search([]).sorted(lambda rec: (rec.sequence, rec.number), reverse=True)
        for records in self:
            records.tags_min_version_excluded_id = False
            if records.tags_min_version_id:
                records.tags_min_version_excluded_id = next((version for version in all_versions if version.number < records.tags_min_version_id.number), False)

    @api.depends('error_content_ids.canonical_tag')
    def _compute_canonical_tags(self):
        for record in self:
            canonical_tags = sorted(set(record.error_content_ids.filtered('canonical_tag').mapped('canonical_tag')))
            record.canonical_tags = ','.join(canonical_tags)

    @api.depends('tags_min_version_id')
    def _compute_tags_min_version_id(self):
        all_versions = self.env['runbot.version'].search([]).sorted(lambda rec: (rec.sequence, rec.number))
        for records in self:
            records.tags_min_version_id = False
            if records.tags_min_version_excluded_id:
                records.tags_min_version_id = next((version for version in all_versions if version.number > records.tags_min_version_excluded_id.number), False)

    @api.depends('build_error_link_ids')
    def _compute_unique_build_error_link_ids(self):
        for record in self:
            seen = set()
            id_list = []
            for error_link in record.build_error_link_ids:
                if error_link.build_id.id not in seen:
                    seen.add(error_link.build_id.id)
                    id_list.append(error_link.id)
            record.unique_build_error_link_ids = record.env['runbot.build.error.link'].browse(id_list)

    @api.depends('name', 'error_content_ids')
    def _compute_description(self):
        for record in self:
            record.description = record.name
            if record.error_content_ids:
                record.description = record.error_content_ids[0].content

    def _compute_content(self):
        for record in self:
            record.content = '\n'.join(record.error_content_ids.mapped('content'))

    def _search_content(self, operator, value):
        return [('error_content_ids', 'any', [('content', operator, value)])]

    @api.depends('error_content_ids')
    def _compute_count(self):
        for record in self:
            record.error_count = len(record.error_content_ids)

    @api.depends('error_content_ids')
    def _compute_random(self):
        for record in self:
            record.random = any(error.random for error in record.error_content_ids)

    @api.depends('error_content_ids.qualifiers')
    def _compute_common_qualifiers(self):
        for record in self:
            qualifiers = defaultdict(set)
            key_count = defaultdict(int)
            for content in record.error_content_ids:
                for key, value in content.qualifiers.dict.items():
                    qualifiers[key].add(value)
                    key_count[key] += 1
            record.common_qualifiers = {k: v.pop() for k, v in qualifiers.items() if len(v) == 1 and key_count[k] == len(record.error_content_ids)}

    @api.depends('error_content_ids.qualifiers')
    def _compute_unique_qualifiers(self):
        for record in self:
            qualifiers = defaultdict(set)
            key_count = defaultdict(int)
            for content in record.error_content_ids:
                for key, value in content.qualifiers.dict.items():
                    qualifiers[key].add(value)
                    key_count[key] += 1
            record.unique_qualifiers = {k: v.pop() for k, v in qualifiers.items() if len(v) == 1}

    @api.depends('common_qualifiers')
    def _compute_similar_ids(self):
        for record in self:
            if record.common_qualifiers:
                query = SQL(
                    r"""SELECT id FROM runbot_build_error WHERE id != %s AND common_qualifiers @> %s""",
                    record.id,
                    json.dumps(record.common_qualifiers.dict),
                )
                self.env.cr.execute(query)
                record.similar_ids = self.env['runbot.build.error'].browse([rec[0] for rec in self.env.cr.fetchall()])
            else:
                record.similar_ids = False

    @api.depends('common_qualifiers')
    def _compute_similar_content_ids(self):
        for record in self:
            if record.common_qualifiers:
                query = SQL(
                    r"""SELECT id FROM runbot_build_error_content WHERE error_id != %s AND qualifiers @> %s""",
                    record.id,
                    json.dumps(record.common_qualifiers.dict),
                )
                self.env.cr.execute(query)
                record.similar_content_ids = self.env['runbot.build.error.content'].browse([rec[0] for rec in self.env.cr.fetchall()])
            else:
                record.similar_content_ids = False

    @api.depends('common_qualifiers')
    def _compute_analogous_ids(self):
        for record in self:
            if record.common_qualifiers:
                query = SQL(
                    r"""SELECT id FROM runbot_build_error WHERE id != %s AND unique_qualifiers @> %s""",
                    record.id,
                    json.dumps(record.unique_qualifiers.dict),
                )
                self.env.cr.execute(query)
                record.analogous_ids = self.env['runbot.build.error'].browse([rec[0] for rec in self.env.cr.fetchall()])
            else:
                record.analogous_ids = False

    @api.depends('common_qualifiers')
    def _compute_analogous_content_ids(self):
        for record in self:
            if record.common_qualifiers:
                query = SQL(
                    r"""SELECT id FROM runbot_build_error_content WHERE error_id != %s AND qualifiers @> %s""",
                    record.id,
                    json.dumps(record.unique_qualifiers.dict),
                )
                self.env.cr.execute(query)
                record.analogous_content_ids = self.env['runbot.build.error.content'].browse([rec[0] for rec in self.env.cr.fetchall()])
            else:
                record.analogous_content_ids = False

    def _get_log_dates(self, start_date: datetime.datetime, end_date: datetime.datetime):
        """
        Returns an count of build_error per hour for the last 30 days.
        -> Dict[Self, Dict[datetime, int]]
        """
        assert self, 'Method does not work if called with empty recordset.'
        result = defaultdict(dict)
        if not self._origin.ids:
            return result
        self.env.cr.execute("""
            SELECT error.id as error_id, date_trunc('hour', link.log_date) as time, count(*) as count
              FROM runbot_build_error AS error
              JOIN runbot_build_error_content AS content ON content.error_id = error.id
              JOIN runbot_build_error_link AS link ON link.error_content_id = content.id
             WHERE error.id IN %s AND link.log_date BETWEEN %s AND %s
          GROUP BY error.id, date_trunc('hour', link.log_date)
        """, (tuple(self.ids), start_date, end_date))
        data = self.env.cr.dictfetchall()
        for d in data:
            result[self.browse(d['error_id'])][d['time']] = d['count']
        return result

    @api.depends('build_error_link_ids')
    def _compute_graph(self):
        end_date = fields.Date.today() + relativedelta(days=1)
        start_date = end_date - relativedelta(days=30)
        log_date_per_error = self._get_log_dates(start_date, end_date)
        for error in self:
            dates = log_date_per_error[error]
            daily_freq = [
                sum(
                    count
                    for hour, count in dates.items() if hour.date() == date.date()
                )
                for date in rrule.rrule(rrule.DAILY, dtstart=start_date, until=end_date)
            ]
            error.graph_history = draw_svg(daily_freq, max_value=max(daily_freq))
            hourly_freq = [
                sum(
                    count
                    for hour, count in dates.items() if hour.hour == h
                )
                for h in range(24)
            ]
            error.graph_hourly_recurence = draw_svg(hourly_freq)
            day_of_week_freq = [
                sum(
                    count
                    for hour, count in dates.items() if hour.isoweekday() -1 == day
                )
                for day in range(7)
            ]
            error.graph_day_of_week_recurence = draw_svg(day_of_week_freq)
            day_of_month_recurrence = [
                sum(
                    count
                    for hour, count in dates.items() if hour.day - 1 == day
                )
                for day in range(31)
            ]
            error.graph_day_of_month_recurence = draw_svg(day_of_month_recurrence)

    @api.constrains('test_tags')
    def _check_test_tags(self):
        for build_error in self:
            if build_error.test_tags and '-' in build_error.test_tags:
                raise ValidationError('Build error test_tags should not be negated')

    @api.onchange('test_tags')
    def _onchange_test_tags(self):
        if self.test_tags and self.version_ids:
            self.tags_min_version_id = min(self.version_ids, key=lambda rec: rec.number)
            self.tags_max_version_id = max(self.version_ids, key=lambda rec: rec.number)

    @api.onchange('customer')
    def _onchange_customer(self):
        if not self.responsible:
            self.responsible = self.customer

    def create(self, vals_list):
        records = super().create(vals_list)
        records.action_assign()
        return records

    def write(self, vals):
        if 'active' in vals:
            for build_error in self:
                if not (self.env.su or self.env.user.has_groups('runbot.group_runbot_admin')):
                    if build_error.test_tags:
                        raise UserError("This error as a test-tag and can only be (de)activated by admin")
                    if not vals['active'] and build_error.active and build_error.last_seen_date and build_error.last_seen_date + relativedelta(days=1) > fields.Datetime.now():
                        raise UserError("This error broke less than one day ago can only be deactivated by admin")
        return super().write(vals)

    def _merge(self, others):
        self.ensure_one
        error = self
        for previous_error in others:
            # todo, check that all relevant fields are checked and transfered/logged
            if previous_error.test_tags and error.test_tags != previous_error.test_tags:
                if previous_error.test_tags and not self.env.su:
                    raise UserError(f"Cannot merge an error with test tags: {previous_error.test_tags}")
                elif not error.test_tags:
                    error.sudo().test_tags = previous_error.test_tags
                    previous_error.sudo().test_tags = False
            if previous_error.responsible:
                if error.responsible and error.responsible != previous_error.responsible and not self.env.su:
                    raise UserError(f"error {error.id} as already a responsible ({error.responsible}) cannot assign {previous_error.responsible}")
                else:
                    error.responsible = previous_error.responsible
            if previous_error.team_id:
                if not error.team_id:
                    error.team_id = previous_error.team_id
            previous_error.error_content_ids.write({'error_id': self})
            previous_error.common_qualifiers = dict()
            previous_error.unique_qualifiers = dict()
            if not previous_error.test_tags:
                previous_error.message_post(body=Markup('Error merged into %s') % error._get_form_link())
                previous_error.active = False

    @api.model
    def _test_tags_list(self, build_id=False):
        version = build_id.params_id.version_id.number if build_id else False
        branches = build_id.create_batch_id.bundle_id.branch_ids if build_id else self.env['runbot.branch']

        def filter_tags(e):
            if e.fixing_pr_id in branches:
                return False
            if version:
                min_v = e.tags_min_version_id.number or ''
                max_v = e.tags_max_version_id.number or '~'
                return min_v <= version and max_v >= version
            return True

        test_tag_list = self.search([('test_tags', '!=', False)]).filtered(filter_tags).mapped('test_tags')
        return [test_tag for error_tags in test_tag_list for test_tag in (error_tags).split(',')]

    @api.model
    def _disabling_tags(self, build_id=False):
        return ['-%s' % tag for tag in self._test_tags_list(build_id)]

    def _get_form_url(self):
        self.ensure_one()
        return url_join(self.get_base_url(), f'/web#id={self.id}&model=runbot.build.error&view_type=form')

    def _get_form_link(self):
        self.ensure_one()
        return Markup('<a href="%s">%s</a>') % (self._get_form_url(), self.id)

    def action_get_build_link_record(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'view_mode': 'list,form,pivot',
            'res_model': 'runbot.build.error.link',
            'domain': [('id', 'in', self.unique_build_error_link_ids.ids)],
            'context': "{'create': False}"
        }

    def action_view_errors(self):
        return {
            'type': 'ir.actions.act_window',
            'views': [(False, 'list'), (False, 'form')],
            'res_model': 'runbot.build.error.content',
            'domain': [('error_id', 'in', self.ids)],
            'context': {'active_test': False},
            'target': 'current',
        }

    def action_view_similary_qualified(self):
        return {
            'type': 'ir.actions.act_window',
            'views': [(False, 'list'), (False, 'form')],
            'res_model': 'runbot.build.error',
            'domain': [('id', 'in', [self.id] + self.similar_ids.ids)],
            'context': {'active_test': False},
            'target': 'current',
            'name': 'Similary Qualified Errors'
        }

    def action_view_similary_qualified_contents(self):
        return {
            'type': 'ir.actions.act_window',
            'views': [(False, 'list'), (False, 'form')],
            'res_model': 'runbot.build.error.content',
            'domain': [('id', 'in', self.similar_content_ids.ids)],
            'context': {'active_test': False},
            'target': 'current',
            'name': 'Similary Qualified Contents'
        }

    def action_view_analogous_qualified(self):
        return {
            'type': 'ir.actions.act_window',
            'views': [(False, 'list'), (False, 'form')],
            'res_model': 'runbot.build.error',
            'domain': [('id', 'in', [self.id] + self.analogous_ids.ids)],
            'context': {'active_test': False},
            'target': 'current',
            'name': 'Similary Qualified Errors'
        }

    def action_view_analogous_qualified_contents(self):
        return {
            'type': 'ir.actions.act_window',
            'views': [(False, 'list'), (False, 'form')],
            'res_model': 'runbot.build.error.content',
            'domain': [('id', 'in', self.analogous_content_ids.ids)],
            'context': {'active_test': False},
            'target': 'current',
            'name': 'Similary Qualified Contents'
        }

    @api.depends('manual_team_id', 'auto_team_id')
    def _compute_team_id(self):
        for error in self:
            error.team_id = error.manual_team_id or error.auto_team_id

    def _inverse_team_id(self):
        self.manual_team_id = self.team_id

    def action_assign(self):
        teams = lazy(self.env['runbot.team'].search, ['|', ('path_glob', '!=', False), ('module_ownership_ids', '!=', False)])
        repos = lazy(self.env['runbot.repo'].search, [])

        def _get_team(*, file_path: str = None, module: str = None): # Get team from file path or module, teams and repos are cached
            team = False
            if module:
                team = teams._get_team_from_module(module)
            if not team and file_path:
                team = teams._get_team(file_path, repos)
            return team

        for error in self:
            for content in error.error_content_ids:
                team = _get_team(
                    file_path=content.file_path,
                    module=content.qualifiers.dict.get('module')
                )
                if team:
                    error.auto_team_id = team
                    break

    def action_copy_canonical_tag(self):
        for record in self:
            if record.canonical_tags:
                record.test_tags = record.canonical_tags
                record._onchange_test_tags()

    @api.model
    def _parse_logs(self, ir_logs):
        if not ir_logs:
            return
        regexes = self.env['runbot.error.regex'].search([])
        search_regs = regexes.filtered(lambda r: r.re_type == 'filter')
        cleaning_regs = regexes.filtered(lambda r: r.re_type == 'cleaning')

        hash_dict = defaultdict(self.env['ir.logging'].browse)
        for log in ir_logs:
            if search_regs._r_search(log.message):
                continue
            fingerprint = self.env['runbot.build.error.content']._digest(cleaning_regs._r_sub(log.message))
            hash_dict[fingerprint] |= log

        build_error_contents = self.env['runbot.build.error.content']
        # add build ids to already detected errors
        existing_errors_contents = self.env['runbot.build.error.content'].search([('fingerprint', 'in', list(hash_dict.keys())), ('error_id.active', '=', True)])
        existing_fingerprints = {error.fingerprint: error for error in existing_errors_contents}
        build_error_contents |= existing_errors_contents
        # create an error for the remaining entries
        for fingerprint, logs in hash_dict.items():
            if fingerprint in existing_fingerprints:
                # metadata update, keep this for a while
                error = existing_fingerprints[fingerprint]
                if not error.metadata and logs[0].metadata:
                    error.metadata = logs[0].metadata

                continue
            new_build_error_content = self.env['runbot.build.error.content'].create({
                'content': logs[0].message,
                'module_name': logs[0].name.removeprefix('odoo.').removeprefix('addons.'),
                'file_path': logs[0].path,
                'function': logs[0].func,
                'metadata': logs[0].metadata,
            })
            build_error_contents |= new_build_error_content
            existing_fingerprints[fingerprint] = new_build_error_content

        for build_error_content in build_error_contents:
            logs = hash_dict[build_error_content.fingerprint]
            for rec in logs:
                if rec.build_id not in build_error_content.build_ids:
                    self.env['runbot.build.error.link'].create({
                        'build_id': rec.build_id.id,
                        'error_content_id': build_error_content.id,
                        'log_date': rec.create_date,
                    })

        if build_error_contents:
            window_action = {
                "type": "ir.actions.act_window",
                "res_model": "runbot.build.error.content",
                "views": [[False, "list"]],
                "domain": [('id', 'in', build_error_contents.ids)]
            }
            if len(build_error_contents) == 1:
                window_action["views"] = [[False, "form"]]
                window_action["res_id"] = build_error_contents.id
            return window_action

    def action_link_errors(self):
        if len(self) < 2:
            return
        # sort self so that the first one is the one that has test tags or responsible, or the oldest.
        self_sorted = self.sorted(lambda error: (not error.test_tags, not error.responsible, error.error_count, error.id))
        base_error = self_sorted[0]
        base_error._merge(self_sorted - base_error)


class BuildErrorContent(models.Model):

    _name = 'runbot.build.error.content'
    _description = "Build error content"

    _inherit = ('mail.thread', 'mail.activity.mixin', 'runbot.build.error.seen.mixin')
    _rec_name = "id"

    error_id = fields.Many2one('runbot.build.error', 'Linked to', index=True, required=True)
    error_display_id = fields.Integer(compute='_compute_error_display_id', string="Error id")
    content = fields.Text('Error message', required=True)
    cleaned_content = fields.Text('Cleaned error message')
    metadata = JsonDictField('Metadata')
    canonical_tag = fields.Char('Canonical tag', compute='_compute_canonical_tag', store=True)
    summary = fields.Char('Content summary', compute='_compute_summary', store=False)
    module_name = fields.Char('Module name')  # name in ir_logging
    file_path = fields.Char('File Path')  # path in ir logging
    function = fields.Char('Function name')  # func name in ir logging
    fingerprint = fields.Char('Error fingerprint', index=True)
    random = fields.Boolean('underterministic error', tracking=True)
    build_error_link_ids = fields.One2many('runbot.build.error.link', 'error_content_id')

    build_ids = fields.Many2many('runbot.build', compute='_compute_build_ids')
    bundle_ids = fields.One2many('runbot.bundle', compute='_compute_bundle_ids')
    version_ids = fields.One2many('runbot.version', compute='_compute_version_ids', string='Versions', search='_search_version')
    trigger_ids = fields.Many2many('runbot.trigger', compute='_compute_trigger_ids', string='Triggers', search='_search_trigger_ids')
    tag_ids = fields.Many2many('runbot.build.error.tag', string='Tags')
    qualifiers = JsonDictField('Qualifiers', index=True)
    similar_ids = fields.One2many('runbot.build.error.content', compute='_compute_similar_ids')

    responsible = fields.Many2one(related='error_id.responsible')
    customer = fields.Many2one(related='error_id.customer')
    team_id = fields.Many2one(related='error_id.team_id')
    fixing_commit = fields.Char(related='error_id.fixing_commit')
    fixing_pr_id = fields.Many2one(related='error_id.fixing_pr_id')
    fixing_pr_alive = fields.Boolean(related='error_id.fixing_pr_alive')
    fixing_pr_url = fields.Char(related='error_id.fixing_pr_url')
    test_tags = fields.Char(related='error_id.test_tags')
    tags_min_version_id = fields.Many2one(related='error_id.tags_min_version_id')
    tags_max_version_id = fields.Many2one(related='error_id.tags_max_version_id')

    def _set_error_history(self):
        for error_content in self:
            if not error_content.error_id.previous_error_id:
                previous_error_content = error_content.search([
                    ('fingerprint', '=', error_content.fingerprint),
                    ('error_id.active', '=', False),
                    ('error_id.id', '!=', error_content.error_id.id or False),
                    ('id', '!=', error_content.id or False),
                ], order="id desc", limit=1)
                if previous_error_content:
                    error_content.error_id.message_post(body=f"An historical error was found for error {error_content.id}: {previous_error_content.id}")
                    error_content.error_id.previous_error_id = previous_error_content.error_id

    @api.model_create_multi
    def create(self, vals_list):
        cleaners = self.env['runbot.error.regex'].search([('re_type', '=', 'cleaning')])
        for vals in vals_list:
            if not vals.get('error_id'):
                # TODO, try to find an existing one that could match, will be done in another pr
                name = vals.get('content', '').split('\n')[0][:1000]
                error = self.env['runbot.build.error'].create({
                    'name': name,
                })
                vals['error_id'] = error.id
            content = vals.get('content')
            cleaned_content = cleaners._r_sub(content)
            vals.update({
                'cleaned_content': cleaned_content,
                'fingerprint': self._digest(cleaned_content)
            })
        records = super().create(vals_list)
        records._set_error_history()
        records.error_id.action_assign()
        return records

    def write(self, vals):
        if 'cleaned_content' in vals:
            vals.update({'fingerprint': self._digest(vals['cleaned_content'])})
        initial_errors = self.mapped('error_id')
        result = super().write(vals)
        if vals.get('error_id'):
            for build_error, previous_error in zip(self, initial_errors):
                if not previous_error.error_content_ids:
                    build_error.error_id._merge(previous_error)
        return result
    
    @api.depends('metadata')
    def _compute_canonical_tag(self):
        for record in self:
            record.canonical_tag = record.metadata.get('test', {}).get('canonical_tag')

    @api.depends('build_error_link_ids')
    def _compute_build_ids(self):
        for record in self:
            record.build_ids = record.build_error_link_ids.mapped('build_id').sorted('id')

    @api.depends('build_ids')
    def _compute_bundle_ids(self):
        for build_error in self:
            top_parent_builds = build_error.build_ids.mapped(lambda rec: rec and rec.top_parent)
            build_error.bundle_ids = top_parent_builds.mapped('slot_ids').mapped('batch_id.bundle_id')

    @api.depends('build_ids')
    def _compute_version_ids(self):
        for build_error in self:
            build_error.version_ids = build_error.build_ids.version_id

    @api.depends('build_ids')
    def _compute_trigger_ids(self):
        for build_error in self:
            build_error.trigger_ids = build_error.build_ids.trigger_id

    @api.depends('content')
    def _compute_summary(self):
        for build_error in self:
            build_error.summary = build_error.content[:80]

    @api.depends('error_id')
    def _compute_error_display_id(self):
        for error_content in self:
            error_content.error_display_id = error_content.error_id.id

    @api.depends('qualifiers')
    def _compute_similar_ids(self):
        """error contents having the exactly the same qualifiers"""
        for record in self:
            if record.qualifiers:
                query = SQL(
                    r"""SELECT id FROM runbot_build_error_content WHERE id != %s AND qualifiers @> %s AND qualifiers <@ %s""",
                    record.id,
                    json.dumps(record.qualifiers.dict),
                    json.dumps(record.qualifiers.dict),
                )
                self.env.cr.execute(query)
                record.similar_ids = self.env['runbot.build.error.content'].browse([rec[0] for rec in self.env.cr.fetchall()])
            else:
                record.similar_ids = False

    @api.model
    def _digest(self, s):
        """
        return a hash 256 digest of the string s
        """
        return hashlib.sha256(s.encode()).hexdigest()

    def _search_version(self, operator, value):
        exclude_domain = []
        if operator == '=':
            exclude_ids = self.env['runbot.build.error'].search([('version_ids', '!=', value)])
            exclude_domain = [('id', 'not in', exclude_ids.ids)]
        return [('build_error_link_ids.version_id', operator, value)] + exclude_domain

    def _search_trigger_ids(self, operator, value):
        return [('build_error_link_ids.trigger_id', operator, value)]

    def _relink(self):
        if len(self) < 2:
            return
        _logger.debug('Relinking error contents %s', self)
        base_error_content = self[0]
        base_error = base_error_content.error_id
        errors = self.env['runbot.build.error']
        links_to_remove = self.env['runbot.build.error.link']
        content_to_remove = self.env['runbot.build.error.content']
        for error_content in self[1:]:
            assert base_error_content.fingerprint == error_content.fingerprint, f'Errors {base_error_content.id} and {error_content.id} have a different fingerprint'
            existing_build_ids = set(base_error_content.build_error_link_ids.build_id.ids)
            links_to_relink = error_content.build_error_link_ids.filtered(lambda rec: rec.build_id.id not in existing_build_ids)
            links_to_remove |= error_content.build_error_link_ids - links_to_relink  # a link already exists to the base error

            links_to_relink.error_content_id = base_error_content

            if error_content.error_id != base_error_content.error_id:
                base_error.message_post(body=Markup('Error content coming from %s was merged into this one') % error_content.error_id._get_form_link())
                if not base_error.active and error_content.error_id.active:
                    base_error.active = True
            errors |= error_content.error_id
            content_to_remove |= error_content
        content_to_remove.unlink()
        links_to_remove.unlink()

        for error in errors:
            error.message_post(body=Markup('Some error contents from this error where moved into %s') % base_error._get_form_link())
            if not error.error_content_ids:
                base_error._merge(error)

    def _get_duplicates(self):
        """ returns a list of lists of duplicates"""
        domain = [('id', 'in', self.ids)] if self else []
        return [r[1] for r in self._read_group(domain, ('fingerprint'), ('id:array_agg'), [('id:count', '>', 1)])]

    def _qualify(self):
        qualify_regexes = self.env['runbot.error.qualify.regex'].search([])
        for record in self:
            all_qualifiers = {}
            for qualify_regex in qualify_regexes:
                res = qualify_regex._qualify(record)
                if res:
                    # res.update({'qualifier_id': qualify_regex.id}) Probably not a good idea
                    all_qualifiers.update(res)
            record.qualifiers = all_qualifiers

    ####################
    #   Actions
    ####################

    def action_link_errors_contents(self):
        """ Link errors with the first one of the recordset
        choosing parent in error with responsible, random bug and finally fisrt seen
        """
        if len(self) < 2:
            return
        # sort self so that the first one is the one that has test tags or responsible, or the oldest.
        self_sorted = self.sorted(lambda ec: (not ec.error_id.test_tags, not ec.error_id.responsible, ec.error_id.error_count, ec.id))
        base_error = self_sorted[0].error_id
        base_error._merge(self_sorted.error_id - base_error)

    def action_clean_content(self):
        _logger.info('Cleaning %s build errorscontent', len(self))
        cleaning_regs = self.env['runbot.error.regex'].search([('re_type', '=', 'cleaning')])

        changed_fingerprints = set()
        for build_error_content in self:
            fingerprint_before = build_error_content.fingerprint
            build_error_content.cleaned_content = cleaning_regs._r_sub(build_error_content.content)
            if fingerprint_before != build_error_content.fingerprint:
                changed_fingerprints.add(build_error_content.fingerprint)

        # merge identical errors
        errors_content_by_fingerprint = self.env['runbot.build.error.content'].search([('fingerprint', 'in', list(changed_fingerprints))])
        to_merge = []
        for fingerprint in changed_fingerprints:
            to_merge.append(errors_content_by_fingerprint.filtered(lambda r: r.fingerprint == fingerprint))
        # this must be done in other iteration since filtered may fail because of unlinked records from _merge
        for errors_content_to_merge in to_merge:
            errors_content_to_merge._relink()

    def action_deduplicate(self):
        rg = self._get_duplicates()
        for ids_list in rg:
            self.env['runbot.build.error.content'].browse(ids_list)._relink()

    def action_find_duplicates(self):
        rg = self._get_duplicates()
        duplicate_ids = []
        for ids_lists in rg:
            duplicate_ids += ids_lists

        return {
            "type": "ir.actions.act_window",
            "res_model": "runbot.build.error.content",
            "domain": [('id', 'in', duplicate_ids)],
            "context": {"create": False, 'group_by': ['fingerprint']},
            "name": "Duplicate Error contents",
            'view_mode': 'list,form'
        }

    def action_qualify(self):
        self._qualify()



class BuildErrorTag(models.Model):

    _name = "runbot.build.error.tag"
    _description = "Build error tag"

    name = fields.Char('Tag')
    error_content_ids = fields.Many2many('runbot.build.error.content', string='Errors')


class ErrorRegex(models.Model):

    _name = "runbot.error.regex"
    _description = "Build error regex"
    _inherit = "mail.thread"
    _rec_name = 'id'
    _order = 'sequence, id'

    regex = fields.Char('Regular expression', tracking=True)
    re_type = fields.Selection([('filter', 'Filter out'), ('cleaning', 'Cleaning')], string="Regex type")
    sequence = fields.Integer('Sequence', default=100)
    replacement = fields.Char('Replacement string', help="String used as a replacment in cleaning. Use '' to remove the matching string. '%' if not set")

    def _r_sub(self, s):
        """ replaces patterns from the recordset by replacement's or '%' in the given string """
        for c in self:
            replacement = c.replacement or '%'
            if c.replacement == "''":
                replacement = ''
            s = re.sub(c.regex, replacement, s)
        return s

    def _r_search(self, s):
        """ Return True if one of the regex is found in s """
        for filter in self:
            if re.search(filter.regex, s):
                return True
        return False


class ErrorBulkWizard(models.TransientModel):
    _name = 'runbot.error.bulk.wizard'
    _description = "Errors Bulk Wizard"

    team_id = fields.Many2one('runbot.team', 'Assigned team')
    responsible_id = fields.Many2one('res.users', 'Assigned fixer')
    fixing_pr_id = fields.Many2one('runbot.branch', 'Fixing PR', domain=[('is_pr', '=', True)])
    fixing_commit = fields.Char('Fixing commit')
    archive = fields.Boolean('Close error (archive)', default=False)
    chatter_comment = fields.Text('Chatter Comment')

    @api.onchange('fixing_commit', 'chatter_comment')
    def _onchange_commit_comment(self):
        if self.fixing_commit or self.chatter_comment:
            self.archive = True

    def action_submit(self):
        error_ids = self.env['runbot.build.error'].browse(self.env.context.get('active_ids'))
        if error_ids:
            if self.team_id:
                error_ids['team_id'] = self.team_id
            if self.responsible_id:
                error_ids['responsible'] = self.responsible_id
            if self.fixing_pr_id:
                error_ids['fixing_pr_id'] = self.fixing_pr_id
            if self.fixing_commit:
                error_ids['fixing_commit'] = self.fixing_commit
            if self.archive:
                error_ids['active'] = False
            if self.chatter_comment:
                for build_error in error_ids:
                    build_error.message_post(body=Markup('%s') % self.chatter_comment, subject="Bullk Wizard Comment")


class ErrorQualifyRegex(models.Model):

    _name = "runbot.error.qualify.regex"
    _description = "Build error qualifying regex"
    _inherit = "mail.thread"
    _rec_name = 'id'
    _order = 'sequence, id'

    sequence = fields.Integer('Sequence', default=100)
    active = fields.Boolean('Active', default=True, tracking=True)
    regex = fields.Char('Regular expression', required=True, tracking=True)

    check_canonical_tag = fields.Boolean('Check canonical tag', default=False, help='Apply regex on canonical tag')
    check_module_name = fields.Boolean('Check Module Name', default=False, help='Apply regex on Error Module Name')
    check_file_path = fields.Boolean('Check File Path', default=False, help='Apply regex on Error Module Name')
    check_function = fields.Boolean('Check Function name', default=False, help='Apply regex on Error Function Name')
    check_content = fields.Boolean('Check content', default=True, help='Apply regex on Error Content')

    check_fields = fields.Char('Checked Fields', compute='_compute_check_fields', help='Fields on which regex is applied')

    test_ids = fields.One2many('runbot.error.qualify.test', 'qualify_regex_id', string="Test Sample", help="Error samples to test qualifying regex")

    def action_generate_fields(self):
        for rec in self:
            for field in list(re.compile(rec.regex).groupindex.keys()):
                existing = self.env['ir.model.fields'].search([('model', '=', 'runbot.build.error.content'), ('name', '=', f'x_{field}')])
                if existing:
                    _logger.info("Field x_%s already exists", field)
                else:
                    _logger.info("Creating field x_%s", field)
                    self.env['ir.model.fields'].create({
                        'model_id': self.env['ir.model']._get('runbot.build.error.content').id,
                        'name': f'x_{field}',
                        'field_description': ' '.join(field.capitalize().split('_')),
                        'ttype': 'char',
                        'required': False,
                        'readonly': True,
                        'store': True,
                        'depends': 'qualifiers',
                        'compute': f"""
for error_content in self:
    error_content['x_{field}'] = error_content.qualifiers.get('{field}', False)""",
                    })

    @api.constrains('regex')
    def _validate(self):
        for rec in self:
            try:
                r = re.compile(rec.regex)
            except re.error as e:
                raise ValidationError("Unable to compile regular expression: %s" % e)
            # verify that a named group exist in the pattern
            if not re.search(r'\(\?P<\w+>.+\)', r.pattern):
                raise ValidationError(
                    "The regular expresion should contain at least one named group pattern e.g: '(?P<module>.+)'"
                )

    @api.depends('check_module_name', 'check_file_path', 'check_function', 'check_content', 'check_canonical_tag')
    def _compute_check_fields(self):
        for record in self:
            res = []
            for cf in ['canonical_tag', 'module_name', 'file_path', 'function', 'content']:
                if record[f'check_{cf}']:
                    res.append(cf)
            record.check_fields = ','.join(res)

    def _qualify(self, build_error_content):
        self.ensure_one()
        content = '\n'.join([(build_error_content[sf] or '') for sf in self.check_fields.split(',') if self.check_fields])
        result = False
        if content and self.regex:
            result = re.search(self.regex, content, flags=re.MULTILINE)
        # filtering empty values to allow non mandatory named groups
        return {k:v for k,v in result.groupdict().items() if v} if result else {}


class QualifyErrorTest(models.Model):
    _name = 'runbot.error.qualify.test'
    _description = 'Extended Relation between a qualify regex and a build error taken as sample'

    qualify_regex_id = fields.Many2one('runbot.error.qualify.regex', required=True)
    error_content_id = fields.Many2one('runbot.build.error.content', string='Content Id', required=True)
    build_error_summary = fields.Char(compute='_compute_summary')
    build_error_content = fields.Text(compute='_compute_content')
    expected_result = JsonDictField('Expected Qualifiers')
    result = JsonDictField('Result', compute='_compute_result')
    is_matching = fields.Boolean(compute='_compute_result', default=False)

    @api.depends('qualify_regex_id.regex', 'error_content_id', 'expected_result', 'result')
    def _compute_result(self):
        for record in self:
            record.result = record.qualify_regex_id._qualify(record.error_content_id)
            record.is_matching = record.result == record.expected_result and record.result != {}

    @api.depends('error_content_id')
    def _compute_summary(self):
        for record in self:
            content = record.error_content_id.content
            record.build_error_summary = content[:70] if content else False

    @api.depends('qualify_regex_id', 'error_content_id')
    def _compute_content(self):
        for record in self:
            record.build_error_content = '\n'.join([record.error_content_id[sf] or '' for sf in record.qualify_regex_id.check_fields.split(',')])
