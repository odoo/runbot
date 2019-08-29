# -*- coding: utf-8 -*-
import hashlib
import logging
import re

from collections import defaultdict
from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class RunbotBuildError(models.Model):

    _name = "runbot.build.error"
    _inherit = "mail.thread"
    _rec_name = "id"

    content = fields.Text('Error message', required=True)
    cleaned_content = fields.Text('Cleaned error message')
    summary = fields.Char('Content summary', compute='_compute_summary', store=False)
    module_name = fields.Char('Module name')  # name in ir_logging
    function = fields.Char('Function name')  # func name in ir logging
    fingerprint = fields.Char('Error fingerprint', index=True)
    random = fields.Boolean('underterministic error', track_visibility='onchange')
    responsible = fields.Many2one('res.users', 'Assigned fixer', track_visibility='onchange')
    fixing_commit = fields.Char('Fixing commit', track_visibility='onchange')
    build_ids = fields.Many2many('runbot.build', 'runbot_build_error_ids_runbot_build_rel', string='Affected builds')
    branch_ids = fields.Many2many('runbot.branch', compute='_compute_branch_ids')
    repo_ids = fields.Many2many('runbot.repo', compute='_compute_repo_ids')
    active = fields.Boolean('Error is not fixed', default=True, track_visibility='onchange')
    tag_ids = fields.Many2many('runbot.build.error.tag', string='Tags')
    build_count = fields.Integer(compute='_compute_build_counts', string='Nb seen', stored=True)
    parent_id = fields.Many2one('runbot.build.error', 'Linked to')

    @api.model
    def create(self, vals):
        cleaners = self.env['runbot.error.regex'].search([('re_type', '=', 'cleaning')])
        content = vals.get('content')
        cleaned_content = cleaners.r_sub('%', content)
        vals.update({'cleaned_content': cleaned_content,
                     'fingerprint': self._digest(cleaned_content)
        })
        return super().create(vals)

    @api.depends('build_ids')
    def _compute_build_counts(self):
        for build_error in self:
            build_error.build_count = len(build_error.build_ids)

    @api.depends('build_ids')
    def _compute_branch_ids(self):
        for build_error in self:
            build_error.branch_ids = build_error.mapped('build_ids.branch_id')

    @api.depends('build_ids')
    def _compute_repo_ids(self):
        for build_error in self:
            build_error.repo_ids = build_error.mapped('build_ids.repo_id')

    @api.depends('content')
    def _compute_summary(self):
        for build_error in self:
            build_error.summary = build_error.content[:50]

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

        hash_dict = defaultdict(list)
        for log in ir_logs:
            if search_regs.r_search(log.message):
                continue
            fingerprint = self._digest(cleaning_regs.r_sub('%', log.message))
            hash_dict[fingerprint].append(log)

        # add build ids to already detected errors
        for build_error in self.env['runbot.build.error'].search([('fingerprint', 'in', list(hash_dict.keys()))]):
            for build in {rec.build_id for rec in hash_dict[build_error.fingerprint]}:
                build.build_error_ids += build_error
            del hash_dict[build_error.fingerprint]

        # create an error for the remaining entries
        for fingerprint, logs in hash_dict.items():
            self.env['runbot.build.error'].create({
                'content': logs[0].message,
                'module_name': logs[0].name,
                'function': logs[0].func,
                'build_ids': [(6, False, [r.build_id.id for r in logs])],
            })

    def link_errors(self):
        """ Link errors with the first one of the recordset
        choosing parent in error with responsible, random bug and finally fisrt seen
        """
        if len(self) < 2:
            return
        build_errors = self.search([('id', 'in', self.ids)], order='responsible asc, random desc, id asc')
        build_errors[1:].parent_id = build_errors[0]



class RunbotBuildErrorTag(models.Model):

    _name = "runbot.build.error.tag"

    name = fields.Char('Tag')
    error_ids = fields.Many2many('runbot.build.error', string='Errors')


class RunbotErrorRegex(models.Model):

    _name = "runbot.error.regex"
    _inherit = "mail.thread"

    regex = fields.Char('Regular expression')
    re_type = fields.Selection([('filter', 'Filter out'), ('cleaning', 'Cleaning')], string="Regex type")

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
