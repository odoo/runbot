import logging

import requests

from odoo import fields, models


class PatchCallback(models.Model):
    _name = 'runbot_merge.patch.callback'
    _inherit = ['runbot_merge.queue.retryable']
    _description = "reports patch success or failure to callback"
    _cron_name = 'runbot_merge.patch_callback_cron'
    _logger = logging.getLogger(__name__)
    RETRY_LIMIT = 5

    patch_id = fields.Many2one('runbot_merge.patch', required=True, ondelete='cascade')
    success = fields.Boolean(help="Whether the patch was successfully applied.")

    def _process_item(self) -> None:
        res = requests.post(self.patch_id.callback_url, params={'success': int(self.success)}, timeout=10)
        self._logger.info(
            "callback of patch %s (%s): %s %s (%s)",
            self.patch_id,
            self.patch_id.callback_url,
            res.status_code,
            res.reason,
            res.content,
        )
        res.raise_for_status()  # trigger log and retry on failure

