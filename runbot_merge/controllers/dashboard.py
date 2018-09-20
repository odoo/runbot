# -*- coding: utf-8 -*-
from odoo.http import Controller, route, request


class MergebotDashboard(Controller):
    @route('/runbot_merge', auth="public", type="http", website=True)
    def dashboard(self):
        return request.render('runbot_merge.dashboard', {
            'projects': request.env['runbot_merge.project'].with_context(active_test=False).sudo().search([]),
        })
