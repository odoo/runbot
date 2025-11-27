from odoo import models, fields


class OAuthProvider(models.Model):
    _inherit = 'auth.oauth.provider'

    required_fields = fields.Char(
        string="Required Fields",
        help="List of fields that are required to sign up a user using this OAuth provider. "
             "Fields should be separated by spaces.",
    )
