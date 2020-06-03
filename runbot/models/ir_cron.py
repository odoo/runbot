import odoo
from dateutil.relativedelta import relativedelta

from odoo import models, fields

odoo.service.server.SLEEP_INTERVAL = 5
odoo.addons.base.models.ir_cron._intervalTypes['seconds'] = lambda interval: relativedelta(seconds=interval)


class ir_cron(models.Model):
    _inherit = "ir.cron"

    interval_type = fields.Selection(selection_add=[('seconds', 'Seconds')])
