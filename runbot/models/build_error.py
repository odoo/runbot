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


class BuildError(models.Model):

    _name = "runbot.build.error"
    _description = "Build error"

    _inherit = ['mail.thread', 'mail.activity.mixin']
    _rec_name = "id"

    content = fields.Text('Error message', required=True)
    cleaned_content = fields.Text('Cleaned error message')
    summary = fields.Char('Content summary', compute='_compute_summary', store=False)
    module_name = fields.Char('Module name')  # name in ir_logging
    file_path = fields.Char('File Path')  # path in ir logging
    function = fields.Char('Function name')  # func name in ir logging
    fingerprint = fields.Char('Error fingerprint', index=True)
    random = fields.Boolean('underterministic error', tracking=True)
    responsible = fields.Many2one('res.users', 'Assigned fixer', tracking=True)
    team_id = fields.Many2one('runbot.team', 'Assigned team', tracking=True)
    fixing_commit = fields.Char('Fixing commit', tracking=True)
    fixing_pr_id = fields.Many2one('runbot.branch', 'Fixing PR', tracking=True, domain=[('is_pr', '=', True)])
    fixing_pr_alive = fields.Boolean('Fixing PR alive', related='fixing_pr_id.alive')
    fixing_pr_url = fields.Char('Fixing PR url', related='fixing_pr_id.branch_url')
    build_ids = fields.Many2many('runbot.build', 'runbot_build_error_ids_runbot_build_rel', string='Affected builds')
    bundle_ids = fields.One2many('runbot.bundle', compute='_compute_bundle_ids')
    version_ids = fields.One2many('runbot.version', compute='_compute_version_ids', string='Versions', search='_search_version')
    trigger_ids = fields.Many2many('runbot.trigger', compute='_compute_trigger_ids', string='Triggers', search='_search_trigger_ids')
    active = fields.Boolean('Active (not fixed)', default=True, tracking=True)
    tag_ids = fields.Many2many('runbot.build.error.tag', string='Tags')
    build_count = fields.Integer(compute='_compute_build_counts', string='Nb seen', store=True)
    parent_id = fields.Many2one('runbot.build.error', 'Linked to', index=True)
    child_ids = fields.One2many('runbot.build.error', 'parent_id', string='Child Errors', context={'active_test': False})
    children_build_ids = fields.Many2many('runbot.build', compute='_compute_children_build_ids', string='Children builds')
    error_history_ids = fields.Many2many('runbot.build.error', compute='_compute_error_history_ids', string='Old errors', context={'active_test': False})
    first_seen_build_id = fields.Many2one('runbot.build', compute='_compute_first_seen_build_id', string='First Seen build')
    first_seen_date = fields.Datetime(string='First Seen Date', related='first_seen_build_id.create_date')
    last_seen_build_id = fields.Many2one('runbot.build', compute='_compute_last_seen_build_id', string='Last Seen build', store=True)
    last_seen_date = fields.Datetime(string='Last Seen Date', related='last_seen_build_id.create_date', store=True)
    test_tags = fields.Char(string='Test tags', help="Comma separated list of test_tags to use to reproduce/remove this error", tracking=True)

    @api.constrains('test_tags')
    def _check_test_tags(self):
        for build_error in self:
            if build_error.test_tags and '-' in build_error.test_tags:
                raise ValidationError('Build error test_tags should not be negated')

    @api.model_create_multi
    def create(self, vals_list):
        cleaners = self.env['runbot.error.regex'].search([('re_type', '=', 'cleaning')])
        for vals in vals_list:
            content = vals.get('content')
            cleaned_content = cleaners.r_sub('%', content)
            vals.update({
                'cleaned_content': cleaned_content,
                'fingerprint': self._digest(cleaned_content)
            })
        records = super().create(vals_list)
        records.assign()
        return records

    def assign(self):
        if not any((not record.responsible and not record.team_id and record.file_path and not record.parent_id) for record in self):
            return
        teams = self.env['runbot.team'].search(['|', ('path_glob', '!=', False), ('module_ownership_ids', '!=', False)])
        repos = self.env['runbot.repo'].search([])
        for record in self:
            if not record.responsible and not record.team_id and record.file_path and not record.parent_id:
                team = teams._get_team(record.file_path, repos)
                if team:
                    record.team_id = team

    def write(self, vals):
        if 'active' in vals:
            for build_error in self:
                (build_error.child_ids - self).write({'active': vals['active']})
                if not (self.env.su or self.user_has_groups('runbot.group_runbot_admin')):
                    if build_error.test_tags:
                        raise UserError("This error as a test-tag and can only be (de)activated by admin")
                    if not vals['active'] and build_error.last_seen_date + relativedelta(days=1) > fields.Datetime.now():
                        raise UserError("This error broke less than one day ago can only be deactivated by admin")
        result = super(BuildError, self).write(vals)
        if vals.get('parent_id'):
            for build_error in self:
                parent = build_error.parent_id
                if build_error.test_tags:
                    if parent.test_tags and not self.env.su:
                        raise UserError(f"Cannot parent an error with test tags: {build_error.test_tags}")
                    elif not parent.test_tags:
                        parent.sudo().test_tags = build_error.test_tags
                        build_error.sudo().test_tags = False
                if build_error.responsible:
                    if parent.responsible and parent.responsible != build_error.responsible and not self.env.su:
                        raise UserError(f"Error {parent.id} as already a responsible ({parent.responsible}) cannot assign {build_error.responsible}")
                    else:
                        parent.responsible = build_error.responsible
                        build_error.responsible = False
                if build_error.team_id:
                    if not parent.team_id:
                        parent.team_id = build_error.team_id
                    build_error.team_id = False
        return result

    @api.depends('build_ids', 'child_ids.build_ids')
    def _compute_build_counts(self):
        for build_error in self:
            build_error.build_count = len(build_error.build_ids | build_error.mapped('child_ids.build_ids'))

    @api.depends('build_ids')
    def _compute_bundle_ids(self):
        for build_error in self:
            top_parent_builds = build_error.build_ids.mapped(lambda rec: rec and rec.top_parent)
            build_error.bundle_ids = top_parent_builds.mapped('slot_ids').mapped('batch_id.bundle_id')

    @api.depends('build_ids', 'child_ids.build_ids')
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

    @api.depends('build_ids', 'child_ids.build_ids')
    def _compute_children_build_ids(self):
        for build_error in self:
            all_builds = build_error.build_ids | build_error.mapped('child_ids.build_ids')
            build_error.children_build_ids = all_builds.sorted(key=lambda rec: rec.id, reverse=True)

    @api.depends('children_build_ids')
    def _compute_last_seen_build_id(self):
        for build_error in self:
            build_error.last_seen_build_id = build_error.children_build_ids and build_error.children_build_ids[0] or False

    @api.depends('children_build_ids')
    def _compute_first_seen_build_id(self):
        for build_error in self:
            build_error.first_seen_build_id = build_error.children_build_ids and build_error.children_build_ids[-1] or False

    @api.depends('fingerprint', 'child_ids.fingerprint')
    def _compute_error_history_ids(self):
        for error in self:
            fingerprints = [error.fingerprint] + [rec.fingerprint for rec in error.child_ids]
            error.error_history_ids = self.search([('fingerprint', 'in', fingerprints), ('active', '=', False), ('id', '!=', error.id or False)])

    @api.model
    def _digest(self, s):
        """
        return a hash 256 digest of the string s
        """
        return hashlib.sha256(s.encode()).hexdigest()

    @api.model
    def _parse_logs(self, ir_logs):
        regexes = self.env['runbot.error.regex'].search([])
        search_regs = regexes.filtered(lambda r: r.re_type == 'filter')
        cleaning_regs = regexes.filtered(lambda r: r.re_type == 'cleaning')

        hash_dict = defaultdict(self.env['ir.logging'].browse)
        for log in ir_logs:
            if search_regs.r_search(log.message):
                continue
            fingerprint = self._digest(cleaning_regs.r_sub('%', log.message))
            hash_dict[fingerprint] |= log

        build_errors = self.env['runbot.build.error']
        # add build ids to already detected errors
        existing_errors = self.env['runbot.build.error'].search([('fingerprint', 'in', list(hash_dict.keys())), ('active', '=', True)])
        build_errors |= existing_errors
        for build_error in existing_errors:
            logs = hash_dict[build_error.fingerprint]
            for build in logs.mapped('build_id'):
                build.build_error_ids += build_error

            # update filepath if it changed. This is optionnal and mainly there in case we adapt the OdooRunner log 
            if logs[0].path != build_error.file_path:
                build_error.file_path = logs[0].path
                build_error.function = logs[0].func

            del hash_dict[build_error.fingerprint]

        # create an error for the remaining entries
        for fingerprint, logs in hash_dict.items():
            build_errors |= self.env['runbot.build.error'].create({
                'content': logs[0].message,
                'module_name': logs[0].name,
                'file_path': logs[0].path,
                'function': logs[0].func,
                'build_ids': [(6, False, [r.build_id.id for r in logs])],
            })

        if build_errors:
            window_action = {
                "type": "ir.actions.act_window",
                "res_model": "runbot.build.error",
                "views": [[False, "tree"]],
                "domain": [('id', 'in', build_errors.ids)]
            }
            if len(build_errors) == 1:
                window_action["views"] = [[False, "form"]]
                window_action["res_id"] = build_errors.id
            return window_action

    def link_errors(self):
        """ Link errors with the first one of the recordset
        choosing parent in error with responsible, random bug and finally fisrt seen
        """
        if len(self) < 2:
            return
        self = self.with_context(active_test=False)
        build_errors = self.search([('id', 'in', self.ids)], order='responsible asc, random desc, id asc')
        build_errors[1:].write({'parent_id': build_errors[0].id})

    def clean_content(self):
        cleaning_regs = self.env['runbot.error.regex'].search([('re_type', '=', 'cleaning')])
        for build_error in self:
            build_error.cleaned_content = cleaning_regs.r_sub('%', build_error.content)

    @api.model
    def test_tags_list(self):
        active_errors = self.search([('test_tags', '!=', False)])
        test_tag_list = active_errors.mapped('test_tags')
        return [test_tag for error_tags in test_tag_list for test_tag in (error_tags).split(',')]

    @api.model
    def disabling_tags(self):
        return ['-%s' % tag for tag in self.test_tags_list()]

    def _search_version(self, operator, value):
        return [('build_ids.version_id', operator, value)]

    def _search_trigger_ids(self, operator, value):
        return [('build_ids.trigger_id', operator, value)]

class BuildErrorTag(models.Model):

    _name = "runbot.build.error.tag"
    _description = "Build error tag"

    name = fields.Char('Tag')
    error_ids = fields.Many2many('runbot.build.error', string='Errors')


class ErrorRegex(models.Model):

    _name = "runbot.error.regex"
    _description = "Build error regex"
    _inherit = "mail.thread"
    _rec_name = 'id'
    _order = 'sequence, id'

    regex = fields.Char('Regular expression')
    re_type = fields.Selection([('filter', 'Filter out'), ('cleaning', 'Cleaning')], string="Regex type")
    sequence = fields.Integer('Sequence', default=100)

    def r_sub(self, replace, s):
        """ replaces patterns from the recordset by replace in the given string """
        for c in self:
            s = re.sub(c.regex, '%', s)
        return s

    def r_search(self, s):
        """ Return True if one of the regex is found in s """
        for filter in self:
            if re.search(filter.regex, s):
                return True
        return False


class ErrorClosingWizard(models.TransientModel):
    _name = 'runbot.error.closing.wizard'
    _description = "Errors Closing Wizard"

    reason = fields.Char("Closing Reason", help="Reason that will appear in the chatter")

    def submit(self):
        error_ids = self.env['runbot.build.error'].browse(self.env.context.get('active_ids'))
        if error_ids:
            for build_error in error_ids:
                build_error.message_post(body=self.reason, subject="Closing Error")
            error_ids['active'] = False


class ErrorReassignWizard(models.TransientModel):
    _name = 'runbot.error.reassign.wizard'
    _description = "Errors reassign Wizard"

    team_id = fields.Many2one('runbot.team', 'Assigned team')
    responsible_id = fields.Many2one('res.users', 'Assigned fixer')
    fixing_pr_id = fields.Many2one('runbot.branch', 'Fixing PR', domain=[('is_pr', '=', True)])
    fixing_commit = fields.Char('Fixing commit')

    def submit(self):
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
