import builtins
import datetime
import logging
import sys

import psycopg2.errors

from odoo import api, fields, models


class FetchJob(models.Model):
    _name = _description = 'runbot_merge.fetch_job'
    _inherit = ['runbot_merge.queue']
    _order = 'commits_at nulls first, id'
    _cron_name = 'runbot_merge.fetch_prs_cron'
    _logger = logging.getLogger(__name__)

    repository = fields.Many2one('runbot_merge.repository', required=True)
    number = fields.Integer(required=True, group_operator=None)
    closing = fields.Boolean(default=False)
    commits_at = fields.Datetime(index="btree_not_null")
    commenter = fields.Char()

    @api.model_create_multi
    def create(self, vals_list):
        now = fields.Datetime.now()
        self.env.ref(self._cron_name)._trigger({
            fields.Datetime.to_datetime(
                vs.get('commits_at') or now
            )
            for vs in vals_list
        })
        return super().create(vals_list)

    def _search_domain(self):
        now = getattr(builtins, 'current_date', None) or fields.Datetime.to_string(datetime.datetime.now())
        return [
            *super()._search_domain(),
            '|', ('commits_at', '=', False),
                 ('commits_at', '<=', now),
        ]

    def _process_item(self):
        self.repository._load_pr(
            self.number,
            closing=self.closing,
            squash=bool(self.commits_at),
            ping=self.commenter and f'@{self.commenter} ',
        )

    def _on_failure(self) -> bool:
        etype, _, _ = sys.exc_info()
        if issubclass(etype, psycopg2.errors.IntegrityError):
            self.commits_at = datetime.datetime.now() + datetime.timedelta(minutes=1)
            return False

        self._logger.exception(
            "Failed to load %s#%s, skipping it",
            self.repository.name,
            self.number,
        )
        return True
