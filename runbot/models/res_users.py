
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models
from odoo.exceptions import ValidationError


class ResUsers(models.Model):
    _inherit = 'res.users'

    runbot_team_ids = fields.Many2many('runbot.team', string="Runbot Teams")
    github_login = fields.Char('Github account')

    _github_login_unique = models.Constraint(
        'unique (github_login)',
        "Github login can only belong to one user",
    )

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS + ['github_login']

    def write(self, values):
        if list(values.keys()) == ['github_login'] and self.env.user.has_group('runbot.group_runbot_team_manager'):
            return super(ResUsers, self.sudo()).write(values)
        return super().write(values)

    def _auth_oauth_validate(self, provider, access_token):
        validation = super()._auth_oauth_validate(provider, access_token)
        provider = self.env['auth.oauth.provider'].browse(provider)
        required_fields = (provider.required_fields or '').split()
        for field in required_fields:
            if not validation.get(field):
                raise ValidationError("The `%s` field is required to sign in." % field)
        return validation

    def _generate_signup_values(self, provider, validation, params):
        signup_values = super()._generate_signup_values(provider, validation, params)
        if 'github_login' in validation:
            signup_values['github_login'] = validation['github_login']
        return signup_values
