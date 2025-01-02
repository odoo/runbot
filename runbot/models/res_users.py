
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    runbot_team_ids = fields.Many2many('runbot.team', string="Runbot Teams")
    github_login = fields.Char('Github account')

    # Add default action_id
    action_id = fields.Many2one('ir.actions.actions',
                                default=lambda self: self.env.ref('runbot.open_view_warning_tree', raise_if_not_found=False))
    build_url_path = fields.Char(related="res_users_settings_id.build_url_path", readonly=False)

    _sql_constraints = [
        (
            "github_login_unique",
            "unique (github_login)",
            "Github login can only belong to one user",
        )
    ]

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS + ['github_login', 'build_url_path']

    def write(self, values):
        if list(values.keys()) == ['github_login'] and self.env.user.has_group('runbot.group_runbot_team_manager'):
            return super(ResUsers, self.sudo()).write(values)
        return super().write(values)


class ResUsersSettings(models.Model):
    _inherit = 'res.users.settings'

    build_url_path = fields.Char("Build run path", default='', help="Default path when entering a running build.")
