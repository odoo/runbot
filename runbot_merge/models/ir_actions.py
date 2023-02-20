import requests
import json

from odoo import models

class ExtendedServerActionContext(models.Model):
    _inherit = 'ir.actions.server'

    def _get_eval_context(self, action=None):
        ctx = super()._get_eval_context(action=action)
        ctx.update(requests=requests.Session(), loads=json.loads, dumps=json.dumps)
        return ctx
