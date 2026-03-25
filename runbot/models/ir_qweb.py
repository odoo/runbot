from ..common import s2human, s2human_long, precise_s2human, transactioncache
from odoo import models, tools
from odoo.http import request
from odoo.addons.website.controllers.main import QueryURL

class IrQweb(models.AbstractModel):
    _inherit = "ir.qweb"

    def _prepare_frontend_environment(self, values):
        response = super()._prepare_frontend_environment(values)
        values['s2human'] = s2human
        values['s2human_long'] = s2human_long
        values['precise_s2human'] = precise_s2human
        return response

    @tools.conditional(
        'xml' in tools.config['dev_mode'],
        transactioncache,
    )  # replace ormcache by transaction cache to avoid reading the same template multiple times in the same requests. Context is ignored but should be the same for each call in the same request
    def _generate_code_cached(self, ref: int):
        return super()._generate_code_cached(ref)
