# -*- coding: utf-8 -*-
import collections
import json
import pathlib

import markdown
import markupsafe
import werkzeug.exceptions

from odoo.http import Controller, route, request

LIMIT = 20
class MergebotDashboard(Controller):
    @route('/runbot_merge', auth="public", type="http", website=True, sitemap=True)
    def dashboard(self):
        projects = request.env['runbot_merge.project'].with_context(active_test=False).sudo().search([])
        stagings = {
            branch: projects.env['runbot_merge.stagings'].search([
                ('target', '=', branch.id)], order='staged_at desc', limit=6)
            for project in projects
            for branch in project.branch_ids
            if branch.active
        }
        prefetch_set = list({
            id
            for stagings in stagings.values()
            for id in stagings.ids
        })
        for st in stagings.values():
            st._prefetch_ids = prefetch_set

        return request.render('runbot_merge.dashboard', {
            'projects': projects,
            'stagings_map': stagings,
        })

    @route('/runbot_merge/<int:branch_id>', auth='public', type='http', website=True, sitemap=False)
    def stagings(self, branch_id, until=None, state=''):
        branch = request.env['runbot_merge.branch'].browse(branch_id).sudo().exists()
        if not branch:
            raise werkzeug.exceptions.NotFound()

        staging_domain = [('target', '=', branch.id)]
        if until:
            staging_domain.append(('staged_at', '<=', until))
        if state:
            staging_domain.append(('state', '=', state))

        stagings = request.env['runbot_merge.stagings'].with_context(active_test=False).sudo().search(staging_domain, order='staged_at desc', limit=LIMIT + 1)

        return request.render('runbot_merge.branch_stagings', {
            'branch': branch,
            'stagings': stagings[:LIMIT],
            'until': until,
            'state': state,
            'next': stagings[-1].staged_at if len(stagings) > LIMIT else None,
        })

    def _entries(self):
        changelog = pathlib.Path(__file__).parent.parent / 'changelog'
        if changelog.is_dir():
            return [
                (d.name, [f.read_text(encoding='utf-8') for f in d.iterdir() if f.is_file()])
                for d in changelog.iterdir()
            ]
        return []

    def entries(self, item_converter):
        entries = collections.OrderedDict()
        for key, items in sorted(self._entries(), reverse=True):
            entries.setdefault(key, []).extend(map(item_converter, items))
        return entries

    @route('/runbot_merge/changelog', auth='public', type='http', website=True, sitemap=True)
    def changelog(self):
        md = markdown.Markdown(extensions=['nl2br'], output_format='html5')
        entries = self.entries(lambda t: markupsafe.Markup(md.convert(t)))
        return request.render('runbot_merge.changelog', {
            'entries': entries,
        })

    @route('/<org>/<repo>/pull/<int(min=1):pr>', auth='public', type='http', website=True, sitemap=False)
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
