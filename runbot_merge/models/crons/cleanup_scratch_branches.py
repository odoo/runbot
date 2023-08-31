import logging

from odoo import models


_logger = logging.getLogger(__name__)
class BranchCleanup(models.TransientModel):
    _name = 'runbot_merge.branch_cleanup'
    _description = "cleans up scratch refs for deactivated branches"

    def _run(self):
        deactivated = self.env['runbot_merge.branch'].search([
            ('active', '=', False),
            ('write_date', '>=', self.env.context['lastcall']),
        ])
        _logger.info(
            "deleting scratch (tmp and staging) refs for branches %s",
            ', '.join(b.name for b in deactivated)
        )
        # loop around the repos first, so we can reuse the gh instance
        for r in deactivated.mapped('project_id.repo_ids'):
            gh = r.github()
            for b in deactivated:
                if b.project_id != r.project_id:
                    continue

                res = gh('delete', f'git/refs/heads/tmp.{b.name}', check=False)
                if res.status_code != 204:
                    _logger.info("no tmp branch found for %s:%s", r.name, b.name)
                res = gh('delete', f'git/refs/heads/staging.{b.name}', check=False)
                if res.status_code != 204:
                    _logger.info("no staging branch found for %s:%s", res.name, b.name)
