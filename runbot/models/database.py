import logging
from odoo import models, fields, api
_logger = logging.getLogger(__name__)


class Database(models.Model):
    _name = 'runbot.database'
    _description = "Database"

    name = fields.Char('Host name', required=True)
    build_id = fields.Many2one('runbot.build', index=True, required=True)
    db_suffix = fields.Char(compute='_compute_db_suffix')

    def _compute_db_suffix(self):
        for record in self:
            record.db_suffix = record.name.replace('%s-' % record.build_id.dest, '')

    @api.model_create_multi
    def create(self, vals_list):
        records = self.browse()
        for vals in vals_list:
            res = self.search([('name', '=', vals['name']), ('build_id', '=', vals['build_id'])])
            if res:
                records |= res
            else:
                records |= super().create(vals)
