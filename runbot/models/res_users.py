
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    runbot_team_ids = fields.Many2many('runbot.team', string="Runbot Teams")
    github_login = fields.Char('Github account')

    _sql_constraints = [
        (
            "github_login_unique",
            "unique (github_login)",
            "Github login can only belong to one user",
        )
    ]

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS + ['github_login']

    def write(self, values):
        if list(values.keys()) == ['github_login'] and self.env.user.has_group('runbot.group_runbot_team_manager'):
            return super(ResUsers, self.sudo()).write(values)
        return super().write(values)

    # backport of 16.0 feature TODO remove after migration
    def _is_internal(self):
        self.ensure_one()
        return not self.sudo().share
