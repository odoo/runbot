
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    runbot_team_ids = fields.Many2many('runbot.team', string="Runbot Teams")
