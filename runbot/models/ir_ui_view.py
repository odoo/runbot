from ..common import s2human, s2human_long
from odoo import models
from odoo.http import request


class IrUiView(models.Model):
    _inherit = ["ir.ui.view"]

    def _prepare_qcontext(self):
        qcontext = super(IrUiView, self)._prepare_qcontext()

        if request and getattr(request, 'is_frontend', False):
            qcontext['s2human'] = s2human
            qcontext['s2human_long'] = s2human_long
        return qcontext
