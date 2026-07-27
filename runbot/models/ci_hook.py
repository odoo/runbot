from odoo import fields, models

class RunbotCiHook(models.Model):
    _name = "runbot.ci_hook"
    _description = "Runbot CI Hook"

    url = fields.Char(string="CI Hook URL", required=True)