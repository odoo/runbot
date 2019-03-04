from email.utils import parseaddr
from odoo import fields, models, tools, api

class Partner(models.Model):
    _inherit = 'res.partner'

    github_login = fields.Char()
    reviewer = fields.Boolean(default=False, help="Can review PRs (maybe m2m to repos/branches?)")
    self_reviewer = fields.Boolean(default=False, help="Can review own PRs (independent from reviewer)")
    delegate_reviewer = fields.Many2many('runbot_merge.pull_requests')
    formatted_email = fields.Char(compute='_rfc5322_formatted')

    def _auto_init(self):
        res = super(Partner, self)._auto_init()
        tools.create_unique_index(
            self._cr, 'runbot_merge_unique_gh_login', self._table, ['github_login'])
        return res

    @api.depends('name', 'email', 'github_login')
    def _rfc5322_formatted(self):
        for partner in self:
            if partner.email:
                email = parseaddr(partner.email)[1]
            elif partner.github_login:
                email = '%s@users.noreply.github.com' % partner.github_login
            else:
                email = ''
            partner.formatted_email = '%s <%s>' % (partner.name, email)
