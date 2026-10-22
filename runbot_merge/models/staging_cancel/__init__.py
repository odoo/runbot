import logging

from odoo import models, fields

_logger = logging.getLogger(__name__)
class CancelWizard(models.TransientModel):
    _name = 'runbot_merge.stagings.cancel'
    _description = "Wizard for cancelling a staging"

    staging_id = fields.Many2one('runbot_merge.stagings', required=True)
    reason = fields.Char()
    retry = fields.Boolean(help="Immediately retry this exact staging (e.g. broken configuration)")
    cancel_splits = fields.Boolean(help="\
        If any split is pending, also cancel them and move the corresponding \
        pull requests back into the general pool.")

    def action_cancel(self):
        staging_id = self.staging_id
        if self.cancel_splits:
            self.env['runbot_merge.split'].search([
                ('target', '=', staging_id.target.id)
            ]).unlink()

        reason = self.reason.replace('%', '%%').strip() if self.reason else ''
        if reason:
            reason = f' because {reason}'
        user = self.env.user
        retry = self.retry
        self.unlink()
        staging_id.cancel(f'Cancelled by {user.display_name}{reason}')
        if retry:
            return staging_id.retry()
        return { 'type': 'ir.actions.act_window_close' }
