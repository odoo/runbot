import logging

from odoo import fields, models
from ... import git


_logger = logging.getLogger(__name__)


class CheckCommits(models.Model):
    _name = 'runbot_merge.pull_requests.check_commits'
    _inherit = ['runbot_merge.queue']
    _description = "uses git to compute a PR's commit count"
    _cron_name = 'runbot_merge.cron_check_commits'
    _logger = _logger

    pull_request_id = fields.Many2one('runbot_merge.pull_requests', required=True)

    def _process_item(self):
        pr_id = self.pull_request_id
        repo = git.get_local(pr_id.repository)
        target_head, pr_head = sorted(
            repo.fetch_heads(
                pr_id.repository,
                f"refs/heads/{pr_id.target.name}",
                pr_id.head,
            ),
            key=lambda h: h == pr_id.head,
        )
        r = repo.stdout().with_config(
            check=True,
            encoding='utf-8',
        ).rev_list('--count', f'{target_head}..{pr_head}')
        self._logger.info("%s: %s commits", pr_id.display_name, r.stdout.strip())
        pr_id.squash = int(r.stdout) == 1

