import random
from email.utils import parseaddr

from odoo import fields, models, tools, api

from .. import github

class CIText(fields.Char):
    type = 'char'
    column_type = ('citext', 'citext')
    column_cast_from = ('varchar', 'text')

class Partner(models.Model):
    _inherit = 'res.partner'

    email = fields.Char(index=True)
    github_login = CIText()
    delegate_reviewer = fields.Many2many('runbot_merge.pull_requests')
    formatted_email = fields.Char(string="commit email", compute='_rfc5322_formatted')
    review_rights = fields.One2many('res.partner.review', 'partner_id')
    override_rights = fields.Many2many('res.partner.override')

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

    def fetch_github_email(self):
        # this requires a token in order to fetch the email field, otherwise
        # it's just not returned, select a random project to fetch
        gh = github.GH(random.choice(self.env['runbot_merge.project'].search([])).github_token, None)
        for p in self.filtered(lambda p: p.github_login and p.email is False):
            p.email = gh.user(p.github_login)['email'] or False
        return False

class PartnerMerge(models.TransientModel):
    _inherit = 'base.partner.merge.automatic.wizard'

    @api.model
    def _update_values(self, src_partners, dst_partner):
        # sift down through src partners, removing all github_login and keeping
        # the last one
        new_login = None
        for p in src_partners:
            new_login = p.github_login or new_login
        if new_login:
            src_partners.write({'github_login': False})
        if new_login and not dst_partner.github_login:
            dst_partner.github_login = new_login
        super()._update_values(src_partners, dst_partner)

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

    def name_get(self):
        return [
            (r.id, '%s: %s' % (r.repository_id.name, ', '.join(filter(None, [
                r.review and "reviewer",
                r.self_review and "self-reviewer"
            ]))))
            for r in self
        ]

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        return self.search((args or []) + [('repository_id.name', operator, name)], limit=limit).name_get()

class OverrideRights(models.Model):
    _name = 'res.partner.override'
    _description = 'lints which the partner can override'

    partner_ids = fields.Many2many('res.partner')
    repository_id = fields.Many2one('runbot_merge.repository')
    context = fields.Char(required=True)

    def init(self):
        super().init()
        tools.create_unique_index(
            self.env.cr, 'res_partner_override_unique', self._table,
            ['context', 'coalesce(repository_id, 0)']
        )

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        return self.search((args or []) + [
            '|', ('context', operator, name),
                 ('repository_id.name', operator, name)
        ], limit=limit).name_get()

    def name_get(self):
        return [
            (r.id, f'{r.repository_id.name}: {r.context}' if r.repository_id else r.context)
            for r in self
        ]
