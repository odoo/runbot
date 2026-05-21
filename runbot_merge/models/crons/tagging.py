import itertools
import json
import logging

from odoo import api, fields, models


class Tagging(models.Model):
    """
    Queue of tag changes to make on PRs.

    Several PR state changes are driven by webhooks, webhooks should return
    quickly, performing calls to the Github API would *probably* get in the
    way of that. Instead, queue tagging changes into this table whose
    execution can be cron-driven.
    """
    _name = _description = 'runbot_merge.pull_requests.tagging'
    _inherit = ['runbot_merge.queue']
    _cron_name = 'runbot_merge.labels_cron'
    _logger = logging.getLogger(__name__)

    repository = fields.Many2one('runbot_merge.repository', required=True)
    # store the PR number (not id) as we need a Tagging for PR objects
    # being deleted (retargeted to non-managed branches)
    pull_request = fields.Integer(group_operator=None)

    tags_add = fields.Char(required=True, default='[]')

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            if not isinstance(values.get('tags_add', ''), str):
                values['tags_add'] = json.dumps(list(values['tags_add']))
        return super().create(vals_list)

    def _process(self):
        """Override to group tagging records by (repository, PR) and process
        one PR's worth of tag changes per invocation.
        """
        item = self.search(self._search_domain(), limit=1)
        if not item:
            return

        # find all sibling tag changes for the same PR
        siblings = self.search([
            ('repository', '=', item.repository.id),
            ('pull_request', '=', item.pull_request),
        ])

        tags_add = set(itertools.chain.from_iterable(
            json.loads(rec.tags_add)
            for rec in siblings
        ))

        gh = item.repository.github()
        try:
            gh.add_tags(item.pull_request, tags_add)
        except Exception:
            self._logger.info(
                "Error while trying to add tags to %s#%s (%s)",
                item.repository.name, item.pull_request, tags_add,
                exc_info=True,
            )
        else:
            siblings.unlink()

        self.env.ref(self._cron_name)._trigger()
