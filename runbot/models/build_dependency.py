from odoo import models, fields


class RunbotBuildDependency(models.Model):
    _name = "runbot.build.dependency"

    build_id = fields.Many2one('runbot.build', 'Build', required=True, ondelete='cascade', index=True)
    dependecy_repo_id = fields.Many2one('runbot.repo', 'Dependency repo', required=True, ondelete='cascade')
    dependency_hash = fields.Char('Name of commit', index=True)
    closest_branch_id = fields.Many2one('runbot.branch', 'Branch', ondelete='cascade')
    match_type = fields.Char('Match Type')

    def _get_repo(self):
        return self.closest_branch_id.repo_id or self.dependecy_repo_id

