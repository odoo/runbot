from email.utils import parseaddr
from odoo import fields, models, tools, api

class Partner(models.Model):
    _inherit = 'res.partner'

    github_login = fields.Char()
    delegate_reviewer = fields.Many2many('runbot_merge.pull_requests')
    formatted_email = fields.Char(string="commit email", compute='_rfc5322_formatted')
    review_rights = fields.One2many('res.partner.review', 'partner_id')

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

class ReviewRights(models.Model):
    _name = 'res.partner.review'
    _description = "mapping of review rights between partners and repos"

    partner_id = fields.Many2one('res.partner', required=True, ondelete='cascade')
    repository_id = fields.Many2one('runbot_merge.repository', required=True)
    review = fields.Boolean(default=False)
    self_review = fields.Boolean(default=False)

    def _auto_init(self):
        res = super()._auto_init()
        tools.create_unique_index(self._cr, 'runbot_merge_review_m2m', self._table, ['partner_id', 'repository_id'])
        return res
