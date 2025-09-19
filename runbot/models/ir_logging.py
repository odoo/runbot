# -*- coding: utf-8 -*-

import logging

from collections import defaultdict

from ..common import pseudo_markdown
from ..fields import JsonDictField
from odoo import models, fields, tools, api
from odoo.exceptions import UserError
from odoo.tools import html_escape

_logger = logging.getLogger(__name__)

TYPES = [(t, t.capitalize()) for t in 'client server runbot subbuild link markdown'.split()]


class IrLogging(models.Model):

    _inherit = "ir.logging"
    _order = 'id'

    build_id = fields.Many2one('runbot.build', 'Build', index=True, ondelete='cascade')
    active_step_id = fields.Many2one('runbot.build.config.step', 'Active step', index=True)
    type = fields.Selection(selection_add=TYPES, string='Type', required=True, index=True, ondelete={t[0]: 'cascade' for t in TYPES})
    error_content_id = fields.Many2one('runbot.build.error.content', compute='_compute_known_error')  # remember to never store this field
    dbname = fields.Char(string='Database Name', index=False)
    metadata = JsonDictField('Metadata')

    @api.model_create_multi
    def create(self, vals_list):
        logs_by_build_id = defaultdict(list)
        for log in vals_list:
            if 'build_id' in log:
                logs_by_build_id[log['build_id']].append(log)

        builds = self.env['runbot.build'].browse(logs_by_build_id.keys())
        for build in builds:
            build_logs = logs_by_build_id[build.id]
            for ir_log in build_logs:
                ir_log['active_step_id'] = build.active_step.id
                if build.local_state != 'running':
                    if ir_log['level'].upper() == 'WARNING':
                        build.local_result = 'warn'
                    elif ir_log['level'].upper() not in ('INFO', 'SEPARATOR', ''):
                        build.local_result = 'ko'
        return super().create(vals_list)

    def _markdown(self):
        """ Apply pseudo markdown parser for message.
        """
        self.ensure_one()
        if self.type != 'markdown':
            _logger.warning('Calling _markdown on a non markdown log')
            return html_escape(self.message)
        return pseudo_markdown(self.message)

    def _compute_known_error(self):
        cleaning_regexes = self.env['runbot.error.regex'].search([('re_type', '=', 'cleaning')])
        fingerprints = defaultdict(list)
        for ir_logging in self:
            ir_logging.error_content_id = False
            if ir_logging.level in ('ERROR', 'CRITICAL', 'WARNING'):
                fingerprints[self.env['runbot.build.error.content']._digest(cleaning_regexes._r_sub(ir_logging.message))].append(ir_logging)
        for build_error_content in self.env['runbot.build.error.content'].search([('fingerprint', 'in', list(fingerprints.keys()))]).sorted(lambda ec: not ec.error_id.active):
            ir_logs = fingerprints[build_error_content.fingerprint]
            for ir_logging in ir_logs:
                ir_logging.error_content_id = build_error_content.id
            if ir_logs:
                fingerprints.pop(build_error_content.fingerprint)

    def _prepare_create_values(self, vals_list):
        # keep the given create date
        result_vals_list = super()._prepare_create_values(vals_list)
        for result_vals, vals in zip(result_vals_list, vals_list):
            if 'create_date' in vals:
                result_vals['create_date'] = vals['create_date']
        return result_vals_list
