import uuid

from odoo import models, fields


class User(models.Model):
    _inherit = 'res.users'

    # Add default action_id
    action_id = fields.Many2one('ir.actions.actions',
                                default=lambda self: self.env.ref('runbot.open_view_warning_tree', raise_if_not_found=False))
    runbot_api_token = fields.Char('API Token', help='The token to use to authenticate against the API')


    def action_generate_token(self):
        self.ensure_one()
        self.runbot_api_token = uuid.uuid4().hex
