# -*- coding: utf-8 -*-

import logging
from odoo import models, fields

_logger = logging.getLogger(__name__)


class runbot_cronbuild(models.Model):
    """ Model that permit to force an extra build based on a daily cron """

    _name = 'runbot.cronbuild'

    name = fields.Char(required=True)
    branch_id = fields.Many2one('runbot.branch', 'Branch', required=True, ondelete='cascade', index=True)
    extra_params = fields.Char('Extra cmd args')
    coverage = fields.Boolean(string='Enable code coverage', default=False)

    def _cron(self):
        """ Generate extra builds when needed (called by cron's)"""
        build_model = self.env['runbot.build']
        for cronbuild in self.search([]):
            last_build = build_model.search([('branch_id', '=', cronbuild.branch_id.id)],
                                            limit=1,
                                            order='sequence desc')
            if last_build:
                build_model.with_context(force_rebuild=True).create({
                    'branch_id': cronbuild.branch_id.id,
                    'name': last_build.name,
                    'author': last_build.author,
                    'author_email': last_build.author_email,
                    'committer': last_build.committer,
                    'committer_email': last_build.committer_email,
                    'subject': cronbuild.name,
                    'modules': last_build.modules,
                    'extra_params': cronbuild.extra_params,
                    'coverage': cronbuild.coverage,
                })
