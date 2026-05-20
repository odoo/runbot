import builtins
import logging
from datetime import datetime

from dateutil import relativedelta

from odoo import models, fields

from ... import git

# how long a merged PR survives
MERGE_AGE = relativedelta.relativedelta(weeks=1)

class DeleteBranches(models.Model):
    _name = 'forwardport.branch_remover'
    _inherit = ['runbot_merge.queue']
    _description = "Removes branches of merged and closed PRs"
    _cron_name = 'runbot_merge.remover'
    _logger = logging.getLogger(__name__)

    pr_id = fields.Many2one('runbot_merge.pull_requests', index=True)

    def _search_domain(self):
        cutoff = getattr(builtins, 'forwardport_merged_before', None) \
             or fields.Datetime.to_string(datetime.now() - MERGE_AGE)
        return [
            '|', ('pr_id.merge_date', '<', cutoff),
                 '&', ('pr_id.closed', '=', True),
                      ('pr_id.write_date', '<', cutoff),
        ]

    def _process_item(self):
        self._logger.info(
            "PR %s: checking deletion of linked branch %s",
            self.pr_id.display_name,
            self.pr_id.label
        )

        if self.pr_id.state not in ('merged', 'closed'):
            self._logger.info('✘ PR is active (%s)', self.pr_id.state)
            return

        repository = self.pr_id.repository
        fp_remote = repository.fp_remote_target
        if not fp_remote:
            self._logger.info('✘ no forward-port target')
            return

        repo_owner, repo_name = fp_remote.split('/')
        owner, branch = self.pr_id.label.split(':')
        if repo_owner != owner:
            self._logger.info('✘ PR owner != FP target owner (%s)', repo_owner)
            return # probably don't have access to arbitrary repos

        r = git.get_local(repository).check(False).push(
            git.fw_url(repository),
            '--delete', branch,
            f'--force-with-lease={branch}:{self.pr_id.head}',
        )
        if r.returncode:
            self._logger.info(
                '✘ failed to delete branch %s of PR %s:\n%s',
                self.pr_id.label,
                self.pr_id.display_name,
                r.stderr.decode(),
            )
        else:
            self._logger.info(
                '✔ deleted branch %s of PR %s',
                self.pr_id.label,
                self.pr_id.display_name,
            )
