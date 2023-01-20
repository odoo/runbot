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

    def action_freeze(self):
        return super(FreezeWizard, self.with_context(forwardport_keep_disabled=True))\
            .action_freeze()

    def unlink(self):
        r = super().unlink()
        if not (self.env.context.get('forwardport_keep_disabled') or self.search_count([])):
            self.env.ref('forwardport.port_forward').active = True
        return r
