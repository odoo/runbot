from odoo import models


class FreezeWizard(models.Model):
    """ Override freeze wizard to disable the forward port cron when one is
    created (so there's a freeze ongoing) and re-enable it once all freezes are
    done.

    If there ever is a case where we have lots of projects,
    """
    _inherit = 'runbot_merge.project.freeze'

    def create(self, vals_list):
        r = super().create(vals_list)
        self.env.ref('forwardport.port_forward').active = False
        return r

    def unlink(self):
        r = super().unlink()
        if not self.search_count([]):
            self.env.ref('forwardport.port_forward').active = True
        return r

    def action_freeze(self):
        # have to store wizard content as it's removed during freeze
        project = self.project_id
        branches_before = project.branch_ids
        prs = self.mapped('release_pr_ids.pr_id')
        r = super().action_freeze()
        new_branch = project.branch_ids - branches_before
        prs.write({'limit_id': new_branch.id})
        return r
