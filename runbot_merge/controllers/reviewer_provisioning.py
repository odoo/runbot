# -*- coding: utf-8 -*-

from odoo import http
from odoo.http import request
try:
    from odoo.addons.saas_worker.util import from_role
except ImportError:
    def from_role(_):
        return lambda _: None


class MergebotReviewerProvisioning(http.Controller):

    @from_role('accounts')
    @http.route(['/runbot_merge/get_reviewers'], type='json', auth='public')
    def fetch_reviewers(self, **kwargs):
        partners = request.env['res.partner'].sudo().search(['|', ('self_reviewer', '=', True), ('reviewer', '=', True)])
        return partners.mapped('github_login')

    @from_role('accounts')
    @http.route(['/runbot_merge/remove_reviewers'], type='json', auth='public', methods=['POST'])
    def update_reviewers(self, github_logins, **kwargs):
        partners = request.env['res.partner'].sudo().search([('github_login', 'in', github_logins)])

        # remove reviewer flag from the partner
        partners.write({
            'reviewer': False,
            'self_reviewer': False,
        })

        # Assign the linked users as portal users
        partners.mapped('user_ids').write({
            'groups_id': [(6, 0, [request.env.ref('base.group_portal').id])]
        })
