from ..common import s2human, s2human_long, precise_s2human
from odoo import models
from odoo.http import request


class IrQweb(models.AbstractModel):
    _inherit = ["ir.qweb"]

    def _prepare_frontend_environment(self, values):
        response = super()._prepare_frontend_environment(values)
        values['s2human'] = s2human
        values['s2human_long'] = s2human_long
        values['precise_s2human'] = precise_s2human
        return response
