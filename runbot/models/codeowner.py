import ast
import re

from odoo import models, fields, api
from odoo.exceptions import ValidationError


class Codeowner(models.Model):
    _name = 'runbot.codeowner'
    _description = "Notify github teams based on filenames regex"
    _inherit = "mail.thread"

    project_id = fields.Many2one('runbot.project', required=True)
    regex = fields.Char('Regular Expression', help='Regex to match full file paths', required=True, tracking=True)
    github_teams = fields.Char(help='Comma separated list of github teams to notify', required=True, tracking=True)
    team_id = fields.Many2one('runbot.team', help='Not mandatory runbot team')
    version_domain = fields.Char('Version Domain', help='Codeowner only applies to the filtered versions')

    @api.constrains('regex')
    def _validate_regex(self):
        for rec in self:
            try:
                r = re.compile(rec.regex)
            except re.error as e:
                raise ValidationError("Unable to compile regular expression: %s" % e)

    @api.constrains('version_domain')
    def _validate_version_domain(self):
        for rec in self:
            try:
                self._match_version(runbot.bundle_master.version_id)
            except Exception as e:
                raise ValidationError("Unable to validate version_domain: %s" % e)

    def _get_version_domain(self):
        """ Helper to get the evaluated version domain """
        self.ensure_one()
        return ast.literal_eval(self.version_domain) if self.version_domain else []

    def _match_version(self, version):
        return version.filtered_domain(self._get_version_domain())
