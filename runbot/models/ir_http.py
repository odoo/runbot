from odoo import models
from odoo.http import request
import threading


class IrHttp(models.AbstractModel):
    _inherit = ["ir.http"]

    @classmethod
    def _dispatch(cls, endpoint):
        result = super()._dispatch(endpoint)
        if request:
            threading.current_thread().user_name = request.env.user.name
        return result
