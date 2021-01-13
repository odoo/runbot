# -*- coding: utf-8 -*-
import json

import werkzeug.exceptions

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

    @route('/<org>/<repo>/pull/<int(min=1):pr>', auth='public', type='http', website=True)
    def pr(self, org, repo, pr):
        pr_id = request.env['runbot_merge.pull_requests'].sudo().search([
            ('repository.name', '=', f'{org}/{repo}'),
            ('number', '=', int(pr)),
        ])
        if not pr_id:
            raise werkzeug.exceptions.NotFound()
        if not pr_id.repository.group_id <= request.env.user.groups_id:
            raise werkzeug.exceptions.NotFound()

        st = {}
        if pr_id.statuses:
            # normalise `statuses` to map to a dict
            st = {
                k: {'state': v} if isinstance(v, str) else v
                for k, v in json.loads(pr_id.statuses_full).items()
            }
        return request.render('runbot_merge.view_pull_request', {
            'pr': pr_id,
            'merged_head': json.loads(pr_id.commits_map).get(''),
            'statuses': st
        })
