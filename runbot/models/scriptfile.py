from odoo import api, fields, models


class ScriptFile(models.Model):
    _name = 'runbot.scriptfile'
    _description = "Script file"
    _inherit = ['mail.thread']

    content = fields.Text('Script File content', required=True, tracking=True)
    dest_path = fields.Char('File Path', required=True)
    copy_name = fields.Char('Docker Copy Name', compute='_compute_copy_name')
    chmod = fields.Char('File permissions', default='754')

    def _compute_display_name(self):
        for sf in self:
            sf.dest_path.split('/')[-1]

    @api.depends('dest_path')
    def _compute_copy_name(self):
        for sf in self:
            sf.copy_name = f'{sf.id}_{sf.dest_path.replace("/", "_")}'
