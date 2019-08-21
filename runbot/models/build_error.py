# -*- coding: utf-8 -*-
import hashlib
import logging
import re

from collections import defaultdict
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

CLEANING_REGS = [
    re.compile(r', line \d+,'),  # simply remove line numbers
]


class RunbotBuildError(models.Model):

    _name = "runbot.build.error"
    _inherit = "mail.thread"

    content = fields.Text('Error message', required=True)
    cleaned_content = fields.Text('Cleaned error message')
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
        content = vals.get('content')
        cleaned_content = self._clean(content)
        vals.update({'cleaned_content': cleaned_content,
                     'fingerprint': self._digest(cleaned_content)
        })
        print(vals)
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

    @api.model
    def _clean(self, s):
        """
        Clean the string s with the cleaning regs
        Replacing the regex with a space
        """
        for r in CLEANING_REGS:
            s = r.sub('%', s)
        return s

    @api.model
    def _digest(self, s):
        """
        return a hash 256 digest of the string s
        """
        return hashlib.sha256(s.encode()).hexdigest()

    @api.model
    def _parse_logs(self, ir_logs):

        hash_dict = defaultdict(list)
        for log in ir_logs:
            fingerprint = self._digest(self._clean(log.message))
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


class RunbotBuildErrorTag(models.Model):

    _name = "runbot.build.error.tag"

    name = fields.Char('Tag')
    error_ids = fields.Many2many('runbot.build.error', string='Errors')
