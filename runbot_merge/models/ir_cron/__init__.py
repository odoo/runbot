import contextvars
import datetime
import math
import time
from typing import Literal
try:
    from typing import Self
except ImportError:
    from typing_extensions import Self

from odoo import models

deactivate = contextvars.ContextVar('deactivate', default=False)


class IrCron(models.Model):
    _inherit = 'ir.cron'

    def _trigger_coalesced(self, *, factor: int) -> Self:
        at = datetime.datetime.fromtimestamp(
            math.ceil(time.time() / factor))
        return self._trigger(at)

    def trigger(self) -> Literal[True]:
        self.check_access_rights('write')
        self._trigger()
        return True

    @classmethod
    def _process_job(cls, db, cron_cr, job):
        def _trampoline():
            try:
                return super(IrCron, cls)._process_job(db, cron_cr, job)
            finally:
                if deactivate.get():
                    cron_cr.execute("UPDATE ir_cron SET active=false WHERE id = %s", [job['id']])

        return contextvars.copy_context().run(_trampoline)

    def _callback(self, cron_name, server_action_id, job_id):
        super(IrCron, self.with_context(deactivate=deactivate.set))\
            ._callback(cron_name, server_action_id, job_id)