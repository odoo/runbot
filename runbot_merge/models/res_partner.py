from odoo import fields, models, tools

class Partner(models.Model):
    _inherit = 'res.partner'

    github_login = fields.Char()
    reviewer = fields.Boolean(default=False, help="Can review PRs (maybe m2m to repos/branches?)")
    self_reviewer = fields.Boolean(default=False, help="Can review own PRs (independent from reviewer)")
    delegate_reviewer = fields.Many2many('runbot_merge.pull_requests')

    def _auto_init(self):
        res = super(Partner, self)._auto_init()
        tools.create_unique_index(
            self._cr, 'runbot_merge_unique_gh_login', self._table, ['github_login'])
        return res
