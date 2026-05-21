import logging

from odoo import fields, models


class IssuesCloser(models.Model):
    _name = 'runbot_merge.issues_closer'
    _inherit = ['runbot_merge.queue']
    _description = "closes issues linked to PRs"
    _cron_name = 'runbot_merge.issues_closer_cron'
    _logger = logging.getLogger(__name__)

    repository_id = fields.Many2one('runbot_merge.repository', required=True)
    number = fields.Integer(required=True)

    def _process_item(self):
        # TODO: batching?
        gh = self.repository_id.github()
        gh('PATCH', f'issues/{self.number}', json={'state': 'closed'}, check=False)
