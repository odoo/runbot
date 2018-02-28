# -*- coding: utf-8 -*-
import werkzeug

from odoo import http
from odoo.addons.http_routing.models.ir_http import slug
from odoo.addons.website.controllers.main import QueryURL
from odoo.http import request
from ..common import uniq_list, flatten, s2human


class Runbot(http.Controller):

    def build_info(self, build):
        real_build = build.duplicate_id if build.state == 'duplicate' else build
        return {
            'id': build.id,
            'name': build.name,
            'state': real_build.state,
            'result': real_build.result,
            'guess_result': real_build.guess_result,
            'subject': build.subject,
            'author': build.author,
            'committer': build.committer,
            'dest': build.dest,
            'real_dest': real_build.dest,
            'job_age': s2human(real_build.job_age),
            'job_time': s2human(real_build.job_time),
            'job': real_build.job,
            'domain': real_build.domain,
            'host': real_build.host,
            'port': real_build.port,
            'server_match': real_build.server_match,
            'duplicate_of': build.duplicate_id if build.state == 'duplicate' else False,
            'coverage': build.branch_id.coverage,
        }

    @http.route(['/runbot', '/runbot/repo/<model("runbot.repo"):repo>'], website=True, auth='public', type='http')
    def repo(self, repo=None, search='', limit='100', refresh='', **kwargs):
        branch_obj = request.env['runbot.branch']
        build_obj = request.env['runbot.build']
        repo_obj = request.env['runbot.repo']

        repo_ids = repo_obj.search([])
        repos = repo_obj.browse(repo_ids)
        if not repo and repos:
            repo = repos[0].id

        context = {
            'repos': repos.ids,
            'repo': repo,
            'host_stats': [],
            'pending_total': build_obj.search_count([('state', '=', 'pending')]),
            'limit': limit,
            'search': search,
            'refresh': refresh,
        }

        build_ids = []
        if repo:
            filters = {key: kwargs.get(key, '1') for key in ['pending', 'testing', 'running', 'done', 'deathrow']}
            domain = [('repo_id', '=', repo.id)]
            domain += [('state', '!=', key) for key, value in iter(filters.items()) if value == '0']
            if search:
                domain += ['|', '|', ('dest', 'ilike', search), ('subject', 'ilike', search), ('branch_id.branch_name', 'ilike', search)]

            build_ids = build_obj.search(domain, limit=int(limit))
            branch_ids, build_by_branch_ids = [], {}

            if build_ids:
                branch_query = """
                SELECT br.id FROM runbot_branch br INNER JOIN runbot_build bu ON br.id=bu.branch_id WHERE bu.id in %s
                ORDER BY bu.sequence DESC
                """
                sticky_dom = [('repo_id', '=', repo.id), ('sticky', '=', True)]
                sticky_branch_ids = [] if search else branch_obj.search(sticky_dom).ids
                request._cr.execute(branch_query, (tuple(build_ids.ids),))
                branch_ids = uniq_list(sticky_branch_ids + [br[0] for br in request._cr.fetchall()])

                build_query = """
                    SELECT 
                        branch_id, 
                        max(case when br_bu.row = 1 then br_bu.build_id end),
                        max(case when br_bu.row = 2 then br_bu.build_id end),
                        max(case when br_bu.row = 3 then br_bu.build_id end),
                        max(case when br_bu.row = 4 then br_bu.build_id end)
                    FROM (
                        SELECT 
                            br.id AS branch_id, 
                            bu.id AS build_id,
                            row_number() OVER (PARTITION BY branch_id) AS row
                        FROM 
                            runbot_branch br INNER JOIN runbot_build bu ON br.id=bu.branch_id 
                        WHERE 
                            br.id in %s
                        GROUP BY br.id, bu.id
                        ORDER BY br.id, bu.id DESC
                    ) AS br_bu
                    WHERE
                        row <= 4
                    GROUP BY br_bu.branch_id;
                """
                request._cr.execute(build_query, (tuple(branch_ids),))
                build_by_branch_ids = {
                    rec[0]: [r for r in rec[1:] if r is not None] for rec in request._cr.fetchall()
                }

            branches = branch_obj.browse(branch_ids)
            build_ids = flatten(build_by_branch_ids.values())
            build_dict = {build.id: build for build in build_obj.browse(build_ids)}

            def branch_info(branch):
                return {
                    'branch': branch,
                    'builds': [self.build_info(build_dict[build_id]) for build_id in build_by_branch_ids[branch.id]]
                }

            context.update({
                'branches': [branch_info(b) for b in branches],
                'testing': build_obj.search_count([('repo_id', '=', repo.id), ('state', '=', 'testing')]),
                'running': build_obj.search_count([('repo_id', '=', repo.id), ('state', '=', 'running')]),
                'pending': build_obj.search_count([('repo_id', '=', repo.id), ('state', '=', 'pending')]),
                'qu': QueryURL('/runbot/repo/' + slug(repo), search=search, limit=limit, refresh=refresh, **filters),
                'filters': filters,
            })

        # consider host gone if no build in last 100
        build_threshold = max(build_ids or [0]) - 100

        for result in build_obj.read_group([('id', '>', build_threshold)], ['host'], ['host']):
            if result['host']:
                context['host_stats'].append({
                    'host': result['host'],
                    'testing': build_obj.search_count([('state', '=', 'testing'), ('host', '=', result['host'])]),
                    'running': build_obj.search_count([('state', '=', 'running'), ('host', '=', result['host'])]),
                })
        return http.request.render('runbot.repo', context)

    @http.route(['/runbot/build/<int:build_id>/kill'], type='http', auth="user", methods=['POST'], csrf=False)
    def build_ask_kill(self, build_id, search=None, **post):
        build = request.env['runbot.build'].browse(build_id)
        build._ask_kill()
        return werkzeug.utils.redirect('/runbot/repo/%s' % build.repo_id.id + ('?search=%s' % search if search else ''))

    @http.route(['/runbot/build/<int:build_id>/force'], type='http', auth="public", methods=['POST'], csrf=False)
    def build_force(self, build_id, search=None, **post):
        build = request.env['runbot.build'].browse(build_id)
        build._force()
        return werkzeug.utils.redirect('/runbot/repo/%s' % build.repo_id.id + ('?search=%s' % search if search else ''))

    @http.route(['/runbot/build/<int:build_id>'], type='http', auth="public", website=True)
    def build(self, build_id, search=None, **post):
        """Events/Logs"""

        Build = request.env['runbot.build']
        Logging = request.env['ir.logging']

        build = Build.browse([build_id])[0]
        if not build.exists():
            return request.not_found()

        real_build = build.duplicate_id if build.state == 'duplicate' else build

        # other builds
        build_ids = Build.search([('branch_id', '=', build.branch_id.id)])
        other_builds = Build.browse(build_ids)
        domain = [('build_id', '=', real_build.id)]
        log_type = request.params.get('type', '')
        if log_type:
            domain.append(('type', '=', log_type))
        level = request.params.get('level', '')
        if level:
            domain.append(('level', '=', level.upper()))
        if search:
            domain.append(('message', 'ilike', search))
        logging_ids = Logging.sudo().search(domain)

        context = {
            'repo': build.repo_id,
            'build': self.build_info(build),
            'br': {'branch': build.branch_id},
            'logs': Logging.sudo().browse(logging_ids).ids,
            'other_builds': other_builds.ids
        }
        return request.render("runbot.build", context)

        @http.route(['/runbot/b/<branch_name>', '/runbot/<model("runbot.repo"):repo>/<branch_name>'], type='http', auth="public", website=True)
        def fast_launch(self, branch_name=False, repo=False, **post):
            """Connect to the running Odoo instance"""
            Build = request.env['runbot.build']

            domain = [('branch_id.branch_name', '=', branch_name)]

            if repo:
                domain.extend([('branch_id.repo_id', '=', repo.id)])
                order = "sequence desc"
            else:
                order = 'repo_id ASC, sequence DESC'

            # Take the 10 lasts builds to find at least 1 running... Else no luck
            builds = Build.search(domain, order=order, limit=10)

            if builds:
                last_build = False
                for build in Build.browse(builds):
                    if build.state == 'running' or (build.state == 'duplicate' and build.duplicate_id.state == 'running'):
                        last_build = build if build.state == 'running' else build.duplicate_id
                        break

                if not last_build:
                    # Find the last build regardless the state to propose a rebuild
                    last_build = Build.browse(builds[0])

                if last_build.state != 'running':
                    url = "/runbot/build/%s?ask_rebuild=1" % last_build.id
                else:
                    url = build.branch_id._get_branch_quickconnect_url(last_build.domain, last_build.dest)[build.branch_id.id]
            else:
                return request.not_found()
            return werkzeug.utils.redirect(url)
