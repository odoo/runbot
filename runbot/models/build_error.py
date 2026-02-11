# -*- coding: utf-8 -*-
import datetime
import hashlib
import json
import logging
import re
from collections import defaultdict

from dateutil import rrule
from dateutil.relativedelta import relativedelta
from markupsafe import Markup

from werkzeug.urls import url_join

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import SQL, lazy, ormcache
from odoo.fields import Domain

from ..fields import JsonDictField
from ..common import transactioncache, TestTagsParser

_logger = logging.getLogger(__name__)


def get_color(value: int, opacity='1'):
    if value >= 10:
        return f'rgba(255, 0, 0, {opacity})'  # red
    elif value >= 5:
        return f'rgba(255, 165, 0, {opacity})'  # orange
    return f'rgba(0, 170, 0, {opacity})'  # green


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

    _error_build_rel_unique = models.Constraint(
        'UNIQUE (build_id, error_content_id)',
        "A link between a build and an error must be unique",
    )

class BuildErrorSeenMixin(models.AbstractModel):
    _name = 'runbot.build.error.seen.mixin'
    _description = "Add last/first build/log_date for error and asssignments"

    first_seen_build_id = fields.Many2one('runbot.build', compute='_compute_seen', string='First Seen build', store=True)
    first_seen_date = fields.Datetime(string='First Seen Date', compute='_compute_seen', store=True)
    last_seen_build_id = fields.Many2one('runbot.build', compute='_compute_seen', string='Last Seen build', store=True)
    last_seen_date = fields.Datetime(string='Last Seen Date', compute='_compute_seen', store=True)
    build_count = fields.Integer(string='Nb Seen', compute='_compute_seen', store=True)
    seen_hash = fields.Char(string='Seen hash', compute='_compute_seen_hash', store=True)
    first_seen_batch_ids = fields.Many2many('runbot.batch', compute='_compute_seen_batch', string='First Seen Batches')
    last_seen_batch_ids = fields.Many2many('runbot.batch', compute='_compute_seen_batch', string='Last Seen Batches')

    history_data = fields.Json('30 days history', compute='_compute_graph')

    @api.depends('build_error_link_ids')
    def _compute_seen(self):
        for record in self:
            record.first_seen_date = False
            record.last_seen_date = False
            record.build_count = 0
            error_link_ids = record.build_error_link_ids.sorted(lambda bel: bel.build_id.top_parent.create_batch_id.id)
            if error_link_ids:
                first_error_link = error_link_ids[0]
                last_error_link = error_link_ids[-1]
                record.first_seen_date = first_error_link.log_date
                record.last_seen_date = last_error_link.log_date
                record.first_seen_build_id = first_error_link.build_id
                record.last_seen_build_id = last_error_link.build_id
                record.build_count = len(error_link_ids.build_id)

    @api.depends('build_error_link_ids')
    def _compute_seen_batch(self):
        from_clause, content_table = self.get_log_dates_from_clause()
        query = f"""
            SELECT record.id, bundle.version_id, MIN(batch.id) AS first_batch_id, MAX(batch.id) AS last_batch_id
            {from_clause}
            JOIN runbot_build_error_link AS link ON link.error_content_id = {content_table}.id
            JOIN runbot_build AS build ON link.build_id = build.id
            JOIN runbot_build_params AS params ON params.id = build.params_id
            JOIN runbot_batch AS batch ON build.create_batch_id = batch.id
            JOIN runbot_bundle AS bundle ON bundle.id = batch.bundle_id
            WHERE record.id IN %s
            GROUP BY record.id, bundle.version_id
        """
        self.env.cr.execute(query, (tuple(self.ids),))
        first_batch_ids_by_record_id = {}
        last_batch_ids_by_record_id = {}
        res = self.env.cr.fetchall()
        for row in res:
            record_id, _version_id, first_batch_id, last_batch_id = row
            first_batch_ids_by_record_id.setdefault(record_id, []).append(first_batch_id)
            last_batch_ids_by_record_id.setdefault(record_id, []).append(last_batch_id)
        for record in self:
            record.first_seen_batch_ids = self.env['runbot.batch'].browse(sorted(first_batch_ids_by_record_id.get(record.id, [])))
            record.last_seen_batch_ids = self.env['runbot.batch'].browse(sorted(last_batch_ids_by_record_id.get(record.id, [])))

    @api.depends('build_error_link_ids')
    def _compute_seen_hash(self):
        for record in self:
            record.seen_hash = False
            if record.build_error_link_ids:
                record.seen_hash = hashlib.sha256(str(sorted(record.build_error_link_ids.build_id.ids)).encode()).hexdigest()

    @api.depends('build_error_link_ids')
    def _compute_graph(self):
        end_date = fields.Date.today()
        start_date = end_date - relativedelta(days=30)
        log_date_per_error = self._get_log_dates(start_date, end_date)
        for error in self:

            fixing_prs = {pr.bundle_id.version_id.id: pr for pr in (error.fixing_pr_id | error.fixing_pr_id.forwardport_ids).filtered('close_date')}
            breaking_prs = {pr.bundle_id.version_id.id: pr for pr in (error.breaking_pr_id | error.breaking_pr_id.forwardport_ids).filtered('close_date')}
            fixing_pr_close_dates = {pr.bundle_id.version_id.id: pr.close_date.strftime("%Y-%m-%d") for pr in fixing_prs.values()}
            breaking_pr_close_dates = {pr.bundle_id.version_id.id: pr.close_date.strftime("%Y-%m-%d") for pr in breaking_prs.values()}
            project_id = error.first_seen_build_id.params_id.project_id.id
            dates = log_date_per_error[error]
            versions = self.env['runbot.bundle'].search([('project_id', '=', project_id), ('sticky', '=', True)]).version_id.sorted(lambda v: (v.sequence, v.number), reverse=True)
            versions_ids = versions.ids
            date_labels = [date.strftime("%Y-%m-%d") for date in rrule.rrule(rrule.DAILY, dtstart=start_date, until=end_date)]
            version_labels = [version.number for version in versions]
            x_indexes = {date: idx for idx, date in enumerate(date_labels)}
            y_indexes = {version_id: idx for idx, version_id in enumerate(versions_ids)}
            daily_version_freq = []
            for date in date_labels:
                daily_version_freq.append([0] * len(version_labels))
            max_count = 0
            for (bundle_create_date, version_id), count in dates.items():
                date_str = bundle_create_date.strftime("%Y-%m-%d")
                # check if the pr close time is after the batch date and move it one day if needed
                # this should be replaced by checking if the pr is in the batch a more reliable way in, the future
                if fixing_pr_close_dates.get(version_id) == date_str and bundle_create_date < fixing_prs[version_id].close_date:
                    fixing_pr_close_dates[version_id] = (fixing_prs[version_id].close_date + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                if breaking_pr_close_dates.get(version_id):
                    breaking_pr = breaking_prs[version_id]
                    breaking_next_day = (breaking_pr.close_date + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                    if breaking_next_day == date_str and bundle_create_date < (breaking_pr.close_date + datetime.timedelta(days=1)):
                        breaking_pr_close_dates['version_id'] = (breaking_pr.close_date + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                x_index = x_indexes.get(date_str)
                y_index = y_indexes.get(version_id)
                if x_index is not None and y_index is not None:
                    c = daily_version_freq[x_index][y_index] + count
                    daily_version_freq[x_index][y_index] = c
                    max_count = max(max_count, c)

            error.history_data = {
                'breaking_pr_close_dates': breaking_pr_close_dates,
                'fixing_pr_close_dates': fixing_pr_close_dates,
                'versions_ids': versions_ids,
                'date_labels': date_labels,
                'version_labels': version_labels,
                'start_date': start_date.strftime("%Y-%m-%d"),
                'end_date': end_date.strftime("%Y-%m-%d"),
                'daily_version_freq': daily_version_freq,
                'max_count': max_count,
                'error_id': error.id,
                'category_id': error.first_seen_build_id.top_parent.params_id.create_batch_id.category_id.id if error.first_seen_build_id else 1,
                'project_id': project_id if error.first_seen_build_id else 1,
            }

    @ormcache('tuple(self.ids)', 'start_date', 'end_date')  # not sure that this is a good idea. Could be a stored field recomputed after scanning the errors?
    def _get_log_dates(self, start_date: datetime.datetime, end_date: datetime.datetime):
        """
        Returns an count of build_error per hour for the last 30 days.
        -> Dict[Self, Dict[datetime, int]]
        """
        assert self, 'Method does not work if called with empty recordset.'
        result = defaultdict(dict)
        if not self._origin.ids:
            return result
        from_clause, content_table = self.get_log_dates_from_clause()
        self.env.cr.execute(f"""
            SELECT record.id as record_id, max(batch.create_date) as bundle_create_date, build.version_id as version_id, count(*) as count
              {from_clause}
              JOIN runbot_build_error_link AS link ON link.error_content_id = {content_table}.id
              JOIN runbot_build AS build ON link.build_id = build.id
              JOIN runbot_batch AS batch ON build.create_batch_id = batch.id
             WHERE record.id IN %s AND batch.create_date BETWEEN %s AND %s
          GROUP BY record.id, date_trunc('day', batch.create_date), build.version_id
        """, (tuple(self.ids), start_date, end_date))
        data = self.env.cr.dictfetchall()
        for d in data:
            result[self.browse(d['record_id'])][d['bundle_create_date'], d['version_id']] = d['count']
        return result

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
    _mail_post_access = 'read'


    name = fields.Char("Name")
    active = fields.Boolean('Open (not fixed)', default=True, tracking=True)
    description = fields.Text("Description", store=True, compute='_compute_description')
    content = fields.Text("Error contents", compute='_compute_content', search="_search_content")
    error_content_ids = fields.One2many('runbot.build.error.content', 'error_id')
    error_count = fields.Integer("Error count", store=True, compute='_compute_count')
    previous_error_id = fields.Many2one('runbot.build.error', string="Already seen error")

    responsible = fields.Many2one('res.users', 'Assigned fixer', tracking=True, domain="[('active', '=', True)]")
    customer = fields.Many2one('res.users', 'Customer', tracking=True)
    team_id = fields.Many2one('runbot.team', 'Assigned team', compute='_compute_team_id', inverse='_inverse_team_id', store=True, tracking=True)
    manual_team_id = fields.Many2one('runbot.team', 'Manually assigned team')
    auto_team_id = fields.Many2one('runbot.team', 'Automatically assigned team', readonly=True) # This is a computed field but not really
    fixing_commit = fields.Char('Fixing commit', tracking=True)
    fixing_pr_id = fields.Many2one('runbot.branch', 'Fixing PR', tracking=True, domain=[('is_pr', '=', True)])
    fixing_pr_alive = fields.Boolean('Fixing PR alive', related='fixing_pr_id.alive')
    fixing_pr_url = fields.Char('Fixing PR url', related='fixing_pr_id.branch_url')
    fixing_bundle_id = fields.Many2one('runbot.bundle', 'Fixing bundle', compute='_compute_fixing_bundle_id', store=True, tracking=True)
    fixing_bundle_url = fields.Char('Fixing bundle url', related='fixing_bundle_id.frontend_url')
    fixing_pr_date = fields.Datetime('Fixing date', related="fixing_pr_id.close_date", help="Date of the merge of the first pr")

    breaking_pr_id = fields.Many2one('runbot.branch', 'Breaking pr', tracking=True, help="Pr that introduced the error")
    breaking_pr_url = fields.Char('Breaking PR url', related='breaking_pr_id.branch_url')
    breaking_bundle_id = fields.Many2one('runbot.bundle', 'Breaking bundle', tracking=True, help="Bundle that introduced the error", related='breaking_pr_id.bundle_id')
    breaking_bundle_url = fields.Char('Breaking bundle url', related='breaking_bundle_id.frontend_url')
    breaking_pr_date = fields.Datetime('Breaking date', related="breaking_pr_id.close_date", help="Date of the merge of the first pr")

    test_tags = fields.Char(string='Test tags', help="Comma separated list of test_tags to use to reproduce/remove this error", tracking=True)
    canonical_tags = fields.Char('Canonical tag', compute='_compute_canonical_tags', store=True)
    tags_match_count = fields.Integer('Nb errors matching the test_tags', compute='_compute_tags_match_count')
    tags_min_version_excluded_id = fields.Many2one('runbot.version', 'Tag min version (excluded)')
    tags_min_version_id = fields.Many2one('runbot.version', 'Tags Min version', compute="_compute_tags_min_version_id", inverse="_inverse_tags_min_version_id", help="Minimal version where the test tags will be applied.", tracking=True)
    tags_max_version_id = fields.Many2one('runbot.version', 'Tags Max version', help="Maximal version where the test tags will be applied.", tracking=True)

    common_qualifiers = JsonDictField('Common Qualifiers', compute='_compute_common_qualifiers', store=True, help="Minimal qualifiers in common needed to link error content.")
    similar_ids = fields.One2many('runbot.build.error', compute='_compute_similar_ids', string="Similar Errors", help="Similar Errors based on common qualifiers")
    similar_content_ids = fields.One2many('runbot.build.error.content', compute='_compute_similar_content_ids', string="Similar Error Contents", help="Similar Error contents based on common qualifiers")
    unique_qualifiers = JsonDictField('Non conflicting Qualifiers', compute='_compute_unique_qualifiers', store=True, help="Non conflicting qualifiers in common needed to link error content.")
    analogous_ids = fields.One2many('runbot.build.error', compute='_compute_analogous_ids', string="Analogous Errors", help="Analogous Errors based on unique qualifiers")
    analogous_content_ids = fields.One2many('runbot.build.error.content', compute='_compute_analogous_content_ids', string="Analogous Error Contents", help="Analogous Error contents based on unique qualifiers")

    # Build error related data
    build_error_link_ids = fields.Many2many('runbot.build.error.link', compute=_compute_related_error_content_ids('build_error_link_ids'), search=_search_related_error_content_ids('build_error_link_ids'))
    unique_build_error_link_ids = fields.Many2many('runbot.build.error.link', compute='_compute_unique_build_error_link_ids')
    build_ids = fields.Many2many('runbot.build', compute=_compute_related_error_content_ids('build_ids'), search=_search_related_error_content_ids('build_ids'))
    bundle_ids = fields.Many2many('runbot.bundle', compute=_compute_related_error_content_ids('bundle_ids'), search=_search_related_error_content_ids('bundle_ids'))
    version_ids = fields.Many2many('runbot.version', string='Versions', compute='_compute_version_ids', search=_search_related_error_content_ids('version_ids'))
    trigger_ids = fields.Many2many('runbot.trigger', string='Triggers', compute=_compute_related_error_content_ids('trigger_ids'), store=True)
    tag_ids = fields.Many2many('runbot.build.error.tag', string='Tags', compute=_compute_related_error_content_ids('tag_ids'), search=_search_related_error_content_ids('tag_ids'))
    random = fields.Boolean('Random', compute="_compute_random", store=True)

    disappearing_batch_ids = fields.Many2many('runbot.batch', compute='_compute_disappearing_batch_ids', string='Fixing batches')

    only_trigger_ids = fields.Many2one('runbot.trigger', string='Only Triggers', compute='_compute_only_trigger_ids', search='_search_only_trigger_ids')
    only_version_ids = fields.Many2one('runbot.version', string='Only Versions', compute='_compute_only_version_ids', search='_search_only_version_ids')

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

    @api.depends('fixing_pr_id')
    def _compute_fixing_bundle_id(self):
        for record in self:
            record.fixing_bundle_id = record.fixing_pr_id.bundle_id if record.fixing_pr_id else False

    @api.depends('error_content_ids.version_ids')
    def _compute_version_ids(self):
        for record in self:
            record['version_ids'] = record.error_content_ids['version_ids'].sorted('number')

    def _compute_disappearing_batch_ids(self):
        # this is really inefficient but should only be used in form view
        # One search per version where it appeared
        # an alternative solution could be to do it using a table, fetching all batches after the minimal
        # last_seen_batches, but it could scale verry bad on old errors
        for record in self:
            disappearing_batches_ids = []
            last_seen_batches = record.last_seen_batch_ids
            for batch in last_seen_batches:
                disappearing_batch = self.env['runbot.batch'].search([
                    ('bundle_id', '=', batch.bundle_id.id),
                    ('id', '>', batch.id),
                    ('category_id', '=', batch.category_id.id),
                    ('state', '=', 'done')], order='id', limit=1)
                if disappearing_batch:
                    disappearing_batches_ids.append(disappearing_batch.id)
            record.disappearing_batch_ids = self.env['runbot.batch'].browse(disappearing_batches_ids)

    def _compute_only_trigger_ids(self):
        for record in self:
            record.only_trigger_ids = record.trigger_ids[0] if record.trigger_ids else False

    def _search_only_trigger_ids(self, operator, value):
        if operator == 'any':
            operator = 'in'
            value = self.env['runbot.trigger'].search(value).ids
        if operator == 'in':
            return ["!", ("trigger_ids", "any", [("id", "not in", value)])]
        raise UserError("Operator %s is not supported for only_trigger_ids search" % operator)

    def _compute_only_version_ids(self):
        for record in self:
            record.only_version_ids = record.version_ids[0] if record.version_ids else False

    def _search_only_version_ids(self, operator, value):
        if operator == 'any':
            operator = 'in'
            value = self.env['runbot.version'].search(value).ids
        if operator == 'in':
            return ["!", ("version_ids", "any", [("id", "not in", value)])]
        raise UserError("Operator %s is not supported for only_version_ids search" % operator)

    def action_appearing_batches(self):
        self.ensure_one()
        if not self.first_seen_batch_ids:
            return
        ids = ','.join(str(i) for i in self.first_seen_batch_ids.ids)
        return {
            'type': 'ir.actions.act_url',
            'url': f'/runbot/batches/ids/{ids}/{self.id}?title=First%20seen%20batches'
            }

    def action_disappearing_batches(self):
        self.ensure_one()
        if not self.disappearing_batch_ids:
            return
        ids = ','.join(str(i) for i in self.disappearing_batch_ids.ids)
        return {
            'type': 'ir.actions.act_url',
            'url': f'/runbot/batches/ids/{ids}/{self.id}?title=Disappearing%20batches',
        }

    def _compute_content(self):
        for record in self:
            record.content = '\n'.join(record.error_content_ids.mapped('content'))

    def _search_content(self, operator, value):
        return [('error_content_ids', 'any', [('content', operator, value)])]

    @api.depends('error_content_ids')
    def _compute_count(self):
        for record in self:
            record.error_count = len(record.error_content_ids)

    @api.depends('error_content_ids.random')
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

    @api.depends('test_tags')
    def _compute_tags_match_count(self):
        for record in self:
            record.tags_match_count = 0
            if record.test_tags:
                tags_parser = TestTagsParser(record.test_tags)
                search_domain = tags_parser.test_tags_to_search_domain(exclude_error_id=record.id)
                if search_domain:
                    record.tags_match_count = self.env['runbot.build.error'].with_context(active_test=True).search_count(search_domain)

    def action_view_impacted_by_tag(self):
        self.ensure_one()
        if not self.test_tags:
            return
        tags_parser = TestTagsParser(self.test_tags)
        return {
            'type': 'ir.actions.act_window',
            'views': [(False, 'list'), (False, 'form')],
            'res_model': 'runbot.build.error',
            'domain': tags_parser.test_tags_to_search_domain(),
            'name': 'Other Errors impacted by test-tag',
            'context': {'active_test': True}
        }

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
                    if not vals['active'] and build_error.active and build_error.last_seen_date and build_error.last_seen_date + relativedelta(days=1) > datetime.datetime.now():
                        raise UserError("This error broke less than one day ago can only be deactivated by admin")

        if (responsible_id := vals.get('responsible')) and vals.get('active', True):
            responsible = self.env['res.users'].browse(responsible_id)
            for build_error in self:
                if build_error.active and responsible != self.env.user:
                    _logger.info('Notifying responsible %s of build error %s', responsible.name, build_error.id)
                    build_error.message_notify(
                        body=f'Error {build_error.id} was assigned to you by {self.env.user.name}',
                        partner_ids=responsible.partner_id.ids,
                        email_layout_xmlid='mail.mail_notification_layout',
                    )
                build_error.message_subscribe(
                    partner_ids=(responsible.partner_id | self.env.user.partner_id).ids,
                )

        return super().write(vals)

    def unlink(self):
        if any(build_error.test_tags for build_error in self):
            raise UserError("Cannot delete errors with test_tags")
        if any((len(build_error.error_content_ids) > 5) for build_error in self):
            raise UserError("Warning: deleting errors with more than 5 contents, please delete them first")
        return super().unlink()

    def get_log_dates_from_clause(self):
        return """
              FROM runbot_build_error AS record
              JOIN runbot_build_error_content AS content ON content.error_id = record.id
        """, 'content'

    def _merge(self, others):
        # TODO xdo split the error id change and other params merge in order to avoid the merge in write and write in merge recursion
        self.ensure_one
        error = self
        fields_to_merge = ['responsible', 'fixing_pr_id', 'breaking_pr_id']
        fields_to_copy = ['manual_team_id']
        for previous_error in others:
            # todo, check that all relevant fields are checked and transfered/logged
            if previous_error.test_tags and error.test_tags != previous_error.test_tags:
                if not error.test_tags:
                    error.sudo().test_tags = previous_error.test_tags
                    previous_error.sudo().test_tags = False
                elif self.env.su:
                    test_tags = error.test_tags.split(',')
                    previous_error
                    for tag in previous_error.test_tags.split(','):
                        if tag not in test_tags:
                            test_tags.append(tag)
                    error.test_tags = ','.join(test_tags)
                    previous_error.test_tags = False
            for field in fields_to_merge + fields_to_copy:
                if previous_error[field]:
                    if field in fields_to_merge and error[field] and error[field] != previous_error[field] and not self.env.su:
                        raise UserError(f"error {error.id} as already a {field} ({error[field]}) cannot assign {previous_error[field]}")
                    if not error[field]:
                        error[field] = previous_error[field]
            previous_error.error_content_ids.with_context(merging=True).write({'error_id': self})
            previous_error.common_qualifiers = dict()
            previous_error.unique_qualifiers = dict()
            previous_error.message_post(body=Markup('Error merged into %s') % error._get_form_link())
            if not previous_error.test_tags:
                previous_error.active = False
        error.message_post(body=Markup('Errors [%s] were merged into this one') % Markup(', ').join([error._get_form_link() for error in others]))

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
            return None
        regexes = self.env['runbot.error.regex'].search([])
        search_regs = regexes.filtered(lambda r: r.re_type == 'filter')
        cleaning_regs = regexes.filtered(lambda r: r.re_type == 'cleaning')

        logs_by_key = defaultdict(self.env['ir.logging'].browse)
        fingerprints = set()
        for log in ir_logs:
            if search_regs._r_search(log.message):
                continue
            fingerprint = self.env['runbot.build.error.content']._digest(cleaning_regs._r_sub(log.message))
            fingerprints.add(fingerprint)
            canonical_tag = log.metadata.get('test', {}).get('canonical_tag', False)
            logs_by_key[fingerprint, canonical_tag] |= log

        build_error_contents = self.env['runbot.build.error.content']
        # add build ids to already detected errors
        existing_errors_contents = self.env['runbot.build.error.content'].search([('fingerprint', 'in', list(fingerprints)), ('error_id.active', '=', True)])
        existing_error_contents_per_key = {(error.fingerprint, error.canonical_tag): error for error in existing_errors_contents}
        build_error_contents |= existing_errors_contents

        # create an error for the remaining entries
        for key, logs in logs_by_key.items():
            for log in logs:
                if key in existing_error_contents_per_key:
                    # metadata update, keep this for a while
                    error = existing_error_contents_per_key[key]
                    if not error.metadata and log.metadata:
                        error.metadata = log.metadata
                    continue
                fingerprint, canonical_tag = key
                new_build_error_content = self.env['runbot.build.error.content'].with_context(mail_create_nosubscribe=True).create({
                    'error_id': None,
                    'content': log.message,
                    'module_name': log.name.removeprefix('odoo.').removeprefix('addons.'),
                    'file_path': log.path,
                    'function': log.func,
                    'metadata': log.metadata,
                    'canonical_tag': canonical_tag,
                    'fingerprint': fingerprint,
                })

                build_error_contents |= new_build_error_content
                existing_error_contents_per_key[key] = new_build_error_content
        for build_error_content in build_error_contents:
            logs = logs_by_key.get((build_error_content.fingerprint, build_error_content.canonical_tag), [])
            for rec in logs:
                if rec.build_id not in build_error_content.build_ids:
                    self.env['runbot.build.error.link'].with_context(mail_create_nosubscribe=True).create({
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
        return None

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

    error_active = fields.Boolean('Active', related='error_id.active')
    error_id = fields.Many2one('runbot.build.error', 'Linked to', index=True, required=True, tracking=True, ondelete='cascade')
    create_error_id = fields.Many2one('runbot.build.error', 'Original error', index=True)
    error_display_id = fields.Integer(compute='_compute_error_display_id', string="Error id")
    content = fields.Text('Error message', required=True)
    cleaned_content = fields.Text('Cleaned error message')
    metadata = JsonDictField('Metadata')
    canonical_tag = fields.Char('Canonical tag', compute='_compute_canonical_tag', store=True, precompute=True)
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
    qualifiers = JsonDictField('Qualifiers')
    similar_ids = fields.One2many('runbot.build.error.content', compute='_compute_similar_ids')
    responsible = fields.Many2one(related='error_id.responsible')
    customer = fields.Many2one(related='error_id.customer')
    team_id = fields.Many2one(related='error_id.team_id')
    fixing_commit = fields.Char(related='error_id.fixing_commit')
    fixing_pr_id = fields.Many2one(related='error_id.fixing_pr_id')
    breaking_pr_id = fields.Many2one(related='error_id.breaking_pr_id')
    fixing_pr_alive = fields.Boolean(related='error_id.fixing_pr_alive')
    fixing_pr_url = fields.Char(related='error_id.fixing_pr_url')
    test_tags = fields.Char(related='error_id.test_tags')
    tags_min_version_id = fields.Many2one(related='error_id.tags_min_version_id')
    tags_max_version_id = fields.Many2one(related='error_id.tags_max_version_id')

    auto_merge_descriptor = fields.Char('Auto merge descriptor', compute='_compute_auto_merge_descriptor')

    def _set_error_history(self):
        for error_content in self:
            if not error_content.error_id.previous_error_id:
                previous_error_content = error_content.search([
                    ('fingerprint', '=', error_content.fingerprint),
                    ('canonical_tag', '=', error_content.canonical_tag),
                    ('error_id.active', '=', False),
                    ('error_id.id', '!=', error_content.error_id.id or False),
                    ('id', '!=', error_content.id or False),
                ], order="id desc", limit=1)
                if previous_error_content:
                    error_content.error_id.with_context(mail_create_nosubscribe=True).message_post(body=f"An historical error was found for error {error_content.id}: {previous_error_content.id}")
                    error_content.error_id.previous_error_id = previous_error_content.error_id

    @transactioncache
    def _get_error_auto_merge(self):
        return self.env['runbot.build.error.merge'].search([('auto_merge', '=', True), ('active', '=', True)])

    @api.model_create_multi
    def create(self, vals_list):
        self = self.with_context(mail_create_nolog=True)  # noqa: PLW0642
        auto_merge = self._get_error_auto_merge()
        cleaners = self.env['runbot.error.regex']._get_cleaners()
        for vals in vals_list:
            self._qualify(vals)  # populate vals with qualifiers
            for k, v in vals['qualifiers'].items():  # this would be done automaticaly bu the compute but is needed to be able to merge vals
                field = f'x_{k}'
                if field in self._fields:
                    vals[field] = v
            if not vals.get('error_id'):
                temp = self.new(vals)  # _get_similar_domain could use any field of the record
                similar_domain = auto_merge._get_similar_domain(temp)
                similar_domain = Domain.AND([similar_domain, [('error_id.active', '=', True)]])
                error_candidates = self.env['runbot.build.error.content'].search(similar_domain, order="id", limit=1)
                if error_candidates:
                    vals['error_id'] = error_candidates[0].error_id.id
                else:
                    name = vals.get('content', '').split('\n')[0][:1000]
                    error = self.env['runbot.build.error'].create({
                        'name': name,
                    })
                    vals['error_id'] = error.id
            vals['create_error_id'] = vals['error_id']
            content = vals.get('content')
            cleaned_content = cleaners._r_sub(content)
            vals.update({
                'cleaned_content': cleaned_content,
                'fingerprint': self._digest(cleaned_content),
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
        if vals.get('error_id') and not self.env.context.get('merging'):
            for build_error, previous_error in zip(self, initial_errors):
                if not previous_error.error_content_ids:
                    build_error.error_id._merge(previous_error)
        return result

    @api.depends_context('error_merge_ids')
    def _compute_auto_merge_descriptor(self):
        error_merge_ids = self.env.context.get('error_merge_ids')
        if error_merge_ids and len(error_merge_ids) == 1:
            error_merge = self.env['runbot.build.error.merge'].browse(error_merge_ids)

        def make_descriptor(content):
            if error_merge_ids:
                return '|'.join([content[f] for f in error_merge.merge_filter_ids.mapped('field_name')])
            return ''
        for record in self:
            record.auto_merge_descriptor = make_descriptor(record)

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
        self.env['runbot.build'].flush_model()
        self.env['runbot.build.error.link'].flush_model()
        self.env.cr.execute(
            """
            SELECT error_content_id, array_agg(distinct runbot_build.version_id)
            FROM runbot_build
            JOIN runbot_build_error_link ON runbot_build_error_link.build_id = runbot_build.id
            WHERE error_content_id IN %s
            GROUP BY error_content_id
            """,
            (tuple(self.ids),),
        )
        res = dict(self.env.cr.fetchall())

        for build_error_content in self:
            build_error_content.version_ids = self.env['runbot.version'].browse([v for v in res.get(build_error_content.id, []) if v]).sorted('number')

    @api.depends('build_ids')
    def _compute_trigger_ids(self):
        for build_error in self:
            build_error.trigger_ids = build_error.build_ids.top_parent.trigger_id

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

    def get_log_dates_from_clause(self):
        return """
              FROM runbot_build_error_content AS record
        """, 'record'

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
            assert base_error_content.canonical_tag == error_content.canonical_tag, f'Errors {base_error_content.id} and {error_content.id} have a different fingerprint'
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
            error.message_post(body=Markup('Error contents from this error were moved into %s') % base_error._get_form_link())
            if not error.error_content_ids:
                base_error._merge(error)

    def _qualify(self, vals=None):
        if vals is None:
            vals = self
        else:
            vals = [vals]
        qualify_regexes = self.env['runbot.error.qualify.regex']._get_cache()
        for record in vals:
            all_qualifiers = {}
            for qualify_regex in qualify_regexes:
                res = qualify_regex._qualify(record)
                if res:
                    # res.update({'qualifier_id': qualify_regex.id}) Probably not a good idea
                    all_qualifiers.update(res)
            record['qualifiers'] = all_qualifiers

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
        return base_error

    def action_extract_errors_contents(self):
        original_errors = self.mapped('error_id')
        new_error = self.env['runbot.build.error'].create({
            'name': self[0].content.split('\n')[0][:1000],
        })
        self.error_id = new_error
        new_error.message_post(body=f"This error was created to extract contents from errors {original_errors.ids}")
        for error in original_errors:
            error.message_post(body=Markup('Some error content where extracted to %s') % new_error._get_form_link())
        _logger.info('Contents %s extracted to error %s', self.ids, new_error.id)
        return {
            'type': 'ir.actions.act_window',
            'views': [(False, 'form')],
            'view_mode': 'form',
            'res_model': 'runbot.build.error',
            'res_id': new_error.id,
        }

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
            errors_with_fingerprint = errors_content_by_fingerprint.filtered(lambda error_content: error_content.fingerprint == fingerprint)
            for canonical_tag in sorted({e.canonical_tag or '' for e in errors_with_fingerprint}):
                to_merge.append(errors_with_fingerprint.filtered(lambda error_content: (error_content.canonical_tag or '') == canonical_tag))
        # this must be done in other iteration since filtered may fail because of unlinked records from _merge
        for errors_content_to_merge in to_merge:
            errors_content_to_merge._relink()

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

    @transactioncache
    def _get_cleaners(self):
        return self.search([('re_type', '=', 'cleaning')])


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

    @transactioncache
    def _get_cache(self):
        return self.env['runbot.error.qualify.regex'].search([])

    def create(self, vals):
        res = super().create(vals)
        self._get_cache.clear_transaction_cache(self)
        return res

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
            if not r.groupindex:
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
        if not self.check_fields:
            return {}
        fields_to_check = self.check_fields.split(',')
        values = build_error_content
        if not isinstance(build_error_content, dict):
            values = build_error_content.read(fields_to_check)[0]
        content = '\n'.join([(values[sf] or '') for sf in fields_to_check])
        result = False
        if content and self.regex:
            result = re.search(self.regex, content, flags=re.MULTILINE)
        # filtering empty values to allow non mandatory named groups
        return {k: v for k, v in result.groupdict().items() if v} if result else {}


class QualifyErrorTest(models.Model):
    _name = 'runbot.error.qualify.test'
    _description = 'Extended Relation between a qualify regex and a build error taken as sample'

    qualify_regex_id = fields.Many2one('runbot.error.qualify.regex', required=True, ondelete='cascade')
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
