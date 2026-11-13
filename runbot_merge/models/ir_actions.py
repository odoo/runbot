import json

import requests

from odoo import models

class ExtendedServerActionContext(models.Model):
    _inherit = 'ir.actions.server'

    def _get_eval_context(self, action=None):
        # hide from pytest as it's not on the PYTHONPATH
        from odoo.addons.saas_worker.auth_util import SaasAuth
        ctx = super()._get_eval_context(action=action)
        session = requests.Session()
        ctx.update(
            requests=session,
            loads=json.loads,
            dumps=json.dumps,
            call_runbot=lambda path, json=None: session.request(
                method='GET' if json is None else 'POST',
                url=f'https://runbot.odoo.com/runbot{path}',
                json=json,
                auth=SaasAuth(),
            )
        )
        return ctx
