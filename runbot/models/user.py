
from odoo import models, fields


class User(models.Model):
    _inherit = 'res.users'

    # Add default action_id
    action_id = fields.Many2one('ir.actions.actions',
                                default=lambda self: self.env.ref('runbot.open_view_warning_tree', raise_if_not_found=False))
