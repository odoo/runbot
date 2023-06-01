import ast
import re

from odoo import models, fields, api
from odoo.exceptions import ValidationError


class Codeowner(models.Model):
    _name = 'runbot.codeowner'
    _description = "Codeowner regex"
    _inherit = "mail.thread"

    project_id = fields.Many2one('runbot.project', required=True, default=lambda self: self.env.ref('runbot.main_project', raise_if_not_found=False))
    regex = fields.Char('Regular Expression', help='Regex to match full file paths', required=True, tracking=True)
    github_teams = fields.Char(help='Comma separated list of github teams to notify', tracking=True)
    team_id = fields.Many2one('runbot.team', help='Not mandatory runbot team', tracking=True)
    version_domain = fields.Char('Version Domain', help='Codeowner only applies to the filtered versions')
    organisation = fields.Char('organisation', related='project_id.organisation')

    @api.constrains('github_teams', 'team_id')
    def _check_team(self):
        for codeowner in self:
            if not codeowner.team_id and not codeowner.github_teams:
                raise ValidationError('Codeowner should at least have a runbot team or a github team')
            if codeowner.team_id and not codeowner.team_id.github_team:
                raise ValidationError('Team %s should have a github team defined to be used in codeowner' % codeowner.team_id.name)

    @api.constrains('regex')
    def _validate_regex(self):
        for rec in self:
            try:
                re.compile(rec.regex)
            except re.error as e:
                raise ValidationError("Unable to compile regular expression: %s" % e)

    @api.constrains('version_domain')
    def _validate_version_domain(self):
        for rec in self:
            try:
                self._match_version(self.env['runbot.version'].search([], limit=1)[0])
            except Exception as e:
                raise ValidationError("Unable to validate version_domain: %s" % e)

    def _get_github_teams(self):
        github_teams = []
        if self.github_teams:
            github_teams = self.github_teams.split(',')
        if self.team_id.github_team:
            github_teams.append(self.team_id.github_team)
        return github_teams

    def _get_version_domain(self):
        """ Helper to get the evaluated version domain """
        self.ensure_one()
        return ast.literal_eval(self.version_domain) if self.version_domain else []

    def _match_version(self, version):
        return not self.version_domain or version.filtered_domain(self._get_version_domain())
