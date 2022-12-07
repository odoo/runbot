import logging

from odoo import models, fields

_logger = logging.getLogger(__name__)
class CancelWizard(models.TransientModel):
    _name = 'runbot_merge.stagings.cancel'
    _description = "Wizard for cancelling a staging"

    staging_id = fields.Many2one('runbot_merge.stagings', required=True)
    reason = fields.Char()
    cancel_splits = fields.Boolean(help="\
        If any split is pending, also cancel them and move the corresponding \
        pull requests back into the general pool.")

    def action_cancel(self):
        if self.cancel_splits:
            self.env['runbot_merge.split'].search([
                ('target', '=', self.staging_id.target.id)
            ]).unlink()

        reason = self.reason.replace('%', '%%').strip() if self.reason else ''
        if reason:
            reason = f' because {reason}'
        self.staging_id.cancel(f'Cancelled by {self.env.user.display_name}{reason}')
        self.unlink()
        return { 'type': 'ir.actions.act_window_close' }
