import logging

from odoo import models
from odoo.addons.runbot_merge import git


_logger = logging.getLogger(__name__)
class BranchCleanup(models.TransientModel):
    _name = 'runbot_merge.branch_cleanup'
    _description = "cleans up scratch refs for deactivated branches"

    def _run(self):
        domain = [('active', '=', False)]
        if lastcall := self.env.context['lastcall']:
            domain.append(('write_date', '>=', lastcall))
        deactivated = self.env['runbot_merge.branch'].search(domain)

        _logger.info(
            "deleting scratch (tmp and staging) refs for branches %s",
            ', '.join(b.name for b in deactivated)
        )
        # loop around the repos first, so we can reuse the gh instance
        for r in deactivated.mapped('project_id.repo_ids'):
            ref_pattern = f'refs/heads/{r.project_id.staging_pattern}'
            refs = (
                ref_pattern % {'stage': stage, 'target': b.name, 'sub': sub}
                for b in deactivated
                if b.project_id == r.project_id
                for stage in ('tmp', 'staging')
                for sub in ('', '1', '2', '3')
                # tmp doesn't have substages so generating those is never useful
                if stage == 'staging' or sub == ''
            )
            # turns out if you delete fully qualified refs they're sent straigt
            # to the remote, you don't get the info that they exist but...
            git.get_local(r).push(
                git.source_url(r),
                '--delete',
                *refs
            )