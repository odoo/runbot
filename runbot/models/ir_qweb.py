from ..common import s2human, s2human_long
from odoo import models
from odoo.http import request


class IrQweb(models.AbstractModel):
    _inherit = ["ir.qweb"]

    def _prepare_frontend_environment(self, values):
        response = super()._prepare_frontend_environment(values)
        values['s2human'] = s2human
        values['s2human_long'] = s2human_long
        return response
