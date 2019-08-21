# -*- coding: utf-8 -*-
from odoo.http import Controller, route, request

LIMIT = 20
class MergebotDashboard(Controller):
    @route('/runbot_merge', auth="public", type="http", website=True)
    def dashboard(self):
        return request.render('runbot_merge.dashboard', {
            'projects': request.env['runbot_merge.project'].with_context(active_test=False).sudo().search([]),
        })

    @route('/runbot_merge/<int:branch_id>', auth='public', type='http', website=True)
    def stagings(self, branch_id, until=None):
        stagings = request.env['runbot_merge.stagings'].with_context(active_test=False).sudo().search([
            ('target', '=', branch_id),
            ('staged_at', '<=', until) if until else (True, '=', True),
        ], order='staged_at desc', limit=LIMIT+1)

        return request.render('runbot_merge.branch_stagings', {
            'branch': request.env['runbot_merge.branch'].browse(branch_id).sudo(),
            'stagings': stagings[:LIMIT],
            'next': stagings[-1].staged_at if len(stagings) > LIMIT else None,
        })
