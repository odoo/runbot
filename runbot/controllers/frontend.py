# -*- coding: utf-8 -*-
import operator
import werkzeug
from collections import OrderedDict

from odoo.addons.http_routing.models.ir_http import slug
from odoo.addons.website.controllers.main import QueryURL
from odoo.http import Controller, request, route
from ..common import uniq_list, flatten, fqdn
from odoo.osv import expression


class Runbot(Controller):

    def _pending(self):
        ICP = request.env['ir.config_parameter'].sudo().get_param
        warn = int(ICP('runbot.pending.warning', 5))
        crit = int(ICP('runbot.pending.critical', 12))
        count = request.env['runbot.build'].search_count([('local_state', '=', 'pending')])
        level = ['info', 'warning', 'danger'][int(count > warn) + int(count > crit)]
        return count, level

    @route(['/runbot', '/runbot/repo/<model("runbot.repo"):repo>'], website=True, auth='public', type='http')
    def repo(self, repo=None, search='', refresh='', **kwargs):
        search = search if len(search) < 60 else search[:60]
        branch_obj = request.env['runbot.branch']
        build_obj = request.env['runbot.build']
        repo_obj = request.env['runbot.repo']

        repo_ids = repo_obj.search([])
        repos = repo_obj.browse(repo_ids)
        if not repo and repos:
            repo = repos[0].id

        pending = self._pending()
        context = {
            'repos': repos.ids,
            'repo': repo,
            'host_stats': [],
            'pending_total': pending[0],
            'pending_level': pending[1],
            'search': search,
            'refresh': refresh,
        }

        build_ids = []
        if repo:
            domain = [('repo_id', '=', repo.id)]
            if search:
                search_domain = []
                for to_search in search.split("|"):
                    search_domain = ['|', '|', '|'] + search_domain
                    search_domain += [('dest', 'ilike', to_search), ('subject', 'ilike', to_search), ('branch_id.branch_name', 'ilike', to_search)]
                domain += search_domain[1:]
            domain = expression.AND([domain, [('hidden', '=', False)]]) # don't display children builds on repo view
            build_ids = build_obj.search(domain, limit=100)
            branch_ids, build_by_branch_ids = [], {}

            if build_ids:
                branch_query = """
                SELECT br.id FROM runbot_branch br INNER JOIN runbot_build bu ON br.id=bu.branch_id WHERE bu.id in %s
                ORDER BY bu.sequence DESC
                """
                sticky_dom = [('repo_id', '=', repo.id), ('sticky', '=', True)]
                sticky_branch_ids = [] if search else branch_obj.search(sticky_dom).sorted(key=lambda b: (b.branch_name == 'master', b.id), reverse=True).ids
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
                            br.id in %s AND (bu.hidden = 'f' OR bu.hidden IS NULL)
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
                    'fqdn': fqdn(),
                    'builds': [build_dict[build_id] for build_id in build_by_branch_ids.get(branch.id) or []]
                }

            context.update({
                'branches': [branch_info(b) for b in branches],
                'testing': build_obj.search_count([('repo_id', '=', repo.id), ('local_state', '=', 'testing')]),
                'running': build_obj.search_count([('repo_id', '=', repo.id), ('local_state', '=', 'running')]),
                'pending': build_obj.search_count([('repo_id', '=', repo.id), ('local_state', '=', 'pending')]),
                'qu': QueryURL('/runbot/repo/' + slug(repo), search=search, refresh=refresh),
                'fqdn': fqdn(),
            })

        # consider host gone if no build in last 100
        build_threshold = max(build_ids or [0]) - 100

        for result in build_obj.read_group([('id', '>', build_threshold)], ['host'], ['host']):
            if result['host']:
                context['host_stats'].append({
                    'host': result['host'],
                    'testing': build_obj.search_count([('local_state', '=', 'testing'), ('host', '=', result['host'])]),
                    'running': build_obj.search_count([('local_state', '=', 'running'), ('host', '=', result['host'])]),
                })

        context.update({'message': request.env['ir.config_parameter'].sudo().get_param('runbot.runbot_message')})
        return request.render('runbot.repo', context)

    @route(['/runbot/build/<int:build_id>/kill'], type='http', auth="user", methods=['POST'], csrf=False)
    def build_ask_kill(self, build_id, search=None, **post):
        build = request.env['runbot.build'].sudo().browse(build_id)
        build._ask_kill()
        return werkzeug.utils.redirect('/runbot/repo/%s' % build.repo_id.id + ('?search=%s' % search if search else ''))

    @route(['/runbot/build/<int:build_id>/wakeup'], type='http', auth="user", methods=['POST'], csrf=False)
    def build_wake_up(self, build_id, search=None, **post):
        build = request.env['runbot.build'].sudo().browse(build_id)
        build._wake_up()
        return werkzeug.utils.redirect('/runbot/repo/%s' % build.repo_id.id + ('?search=%s' % search if search else ''))

    @route([
        '/runbot/build/<int:build_id>/force',
        '/runbot/build/<int:build_id>/force/<int:exact>',
    ], type='http', auth="public", methods=['POST'], csrf=False)
    def build_force(self, build_id, exact=0, search=None, **post):
        build = request.env['runbot.build'].sudo().browse(build_id)
        build._force(exact=bool(exact))
        return werkzeug.utils.redirect('/runbot/repo/%s' % build.repo_id.id + ('?search=%s' % search if search else ''))

    @route(['/runbot/build/<int:build_id>'], type='http', auth="public", website=True)
    def build(self, build_id, search=None, **post):
        """Events/Logs"""

        Build = request.env['runbot.build']
        Logging = request.env['ir.logging']

        build = Build.browse([build_id])[0]
        if not build.exists():
            return request.not_found()

        show_rebuild_button = Build.search([('branch_id', '=', build.branch_id.id), ('parent_id', '=', False)], limit=1) == build

        context = {
            'repo': build.repo_id,
            'build': build,
            'fqdn': fqdn(),
            'br': {'branch': build.branch_id},
            'show_rebuild_button': show_rebuild_button,
        }
        return request.render("runbot.build", context)

    @route(['/runbot/quick_connect/<model("runbot.branch"):branch>'], type='http', auth="public", website=True)
    def fast_launch(self, branch, **post):
        """Connect to the running Odoo instance"""
        Build = request.env['runbot.build']
        domain = [('branch_id', '=', branch.id), ('config_id', '=', branch.config_id.id)]

        # Take the 10 lasts builds to find at least 1 running... Else no luck
        builds = Build.search(domain, order='sequence desc', limit=10)

        if builds:
            last_build = False
            for build in builds:
                if build.real_build.local_state == 'running':
                    last_build = build.real_build
                    break

            if not last_build:
                # Find the last build regardless the state to propose a rebuild
                last_build = builds[0]

            if last_build.local_state != 'running':
                url = "/runbot/build/%s?ask_rebuild=1" % last_build.id
            else:
                url = "http://%s/web/login?db=%s-all&login=admin&redirect=/web?debug=1" % (last_build.domain, last_build.dest)
        else:
            return request.not_found()
        return werkzeug.utils.redirect(url)

    @route(['/runbot/dashboard'], type='http', auth="public", website=True)
    def dashboard(self, refresh=None):
        cr = request.cr
        RB = request.env['runbot.build']
        repos = request.env['runbot.repo'].search([])   # respect record rules

        cr.execute("""SELECT bu.id
                        FROM runbot_branch br
                        JOIN LATERAL (SELECT *
                                        FROM runbot_build bu
                                       WHERE bu.branch_id = br.id
                                    ORDER BY id DESC
                                       LIMIT 3
                                     ) bu ON (true)
                        JOIN runbot_repo r ON (r.id = br.repo_id)
                       WHERE br.sticky
                         AND br.repo_id in %s
                    ORDER BY r.sequence, r.name, br.branch_name, bu.id DESC
                   """, [tuple(repos._ids)])

        builds = RB.browse(map(operator.itemgetter(0), cr.fetchall()))

        count = RB.search_count
        pending = self._pending()
        qctx = {
            'refresh': refresh,
            'host_stats': [],
            'pending_total': pending[0],
            'pending_level': pending[1],
        }

        repos_values = qctx['repo_dict'] = OrderedDict()
        for build in builds:
            repo = build.repo_id
            branch = build.branch_id
            r = repos_values.setdefault(repo.id, {'branches': OrderedDict()})
            if 'name' not in r:
                r.update({
                    'name': repo.name,
                    'base': repo.base,
                    'testing': count([('repo_id', '=', repo.id), ('local_state', '=', 'testing')]),
                    'running': count([('repo_id', '=', repo.id), ('local_state', '=', 'running')]),
                    'pending': count([('repo_id', '=', repo.id), ('local_state', '=', 'pending')]),
                })
            b = r['branches'].setdefault(branch.id, {'name': branch.branch_name, 'builds': list()})
            b['builds'].append(build)

        # consider host gone if no build in last 100
        build_threshold = max(builds.ids or [0]) - 100
        for result in RB.read_group([('id', '>', build_threshold)], ['host'], ['host']):
            if result['host']:
                qctx['host_stats'].append({
                    'fqdn': fqdn(),
                    'host': result['host'],
                    'testing': count([('local_state', '=', 'testing'), ('host', '=', result['host'])]),
                    'running': count([('local_state', '=', 'running'), ('host', '=', result['host'])]),
                })

        return request.render("runbot.sticky-dashboard", qctx)

    def _glances_ctx(self):
        repos = request.env['runbot.repo'].search([])   # respect record rules
        default_config_id = request.env.ref('runbot.runbot_build_config_default').id
        query = """
            SELECT split_part(r.name, ':', 2),
                   br.branch_name,
                   (array_agg(bu.global_result order by bu.id desc))[1]
              FROM runbot_build bu
              JOIN runbot_branch br on (br.id = bu.branch_id)
              JOIN runbot_repo r on (r.id = br.repo_id)
             WHERE br.sticky
               AND br.repo_id in %s
               AND (bu.hidden = 'f' OR bu.hidden IS NULL)
               AND (
                    bu.global_state in ('running', 'done')
               )
               AND bu.global_result not in ('skipped', 'manually_killed')
               AND (bu.config_id = r.repo_config_id
                    OR bu.config_id =  br.branch_config_id
                    OR bu.config_id =  %s)
          GROUP BY 1,2,r.sequence,br.id
          ORDER BY r.sequence, (br.branch_name='master'), br.id
        """
        cr = request.env.cr
        cr.execute(query, (tuple(repos.ids), default_config_id))
        ctx = OrderedDict()
        for row in cr.fetchall():
            ctx.setdefault(row[0], []).append(row[1:])
        return ctx

    @route('/runbot/glances', type='http', auth='public', website=True)
    def glances(self, refresh=None):
        glances_ctx = self._glances_ctx()
        pending = self._pending()
        qctx = {
            'refresh': refresh,
            'pending_total': pending[0],
            'pending_level': pending[1],
            'glances_data': glances_ctx,
        }
        return request.render("runbot.glances", qctx)

    @route('/runbot/monitoring', type='http', auth='user', website=True)
    def monitoring(self, refresh=None):
        glances_ctx = self._glances_ctx()
        pending = self._pending()
        hosts_data = request.env['runbot.host'].search([])

        monitored_config_id = int(request.env['ir.config_parameter'].sudo().get_param('runbot.monitored_config_id', 1))
        request.env.cr.execute("""SELECT DISTINCT ON (branch_id) branch_id, id FROM runbot_build
                                WHERE config_id = %s
                                AND global_state in ('running', 'done')
                                AND branch_id in (SELECT id FROM runbot_branch where sticky='t')
                                AND local_state != 'duplicate'
                                ORDER BY branch_id ASC, id DESC""", [int(monitored_config_id)])
        last_monitored = request.env['runbot.build'].browse([r[1] for r in request.env.cr.fetchall()])

        qctx = {
            'refresh': refresh,
            'pending_total': pending[0],
            'pending_level': pending[1],
            'glances_data': glances_ctx,
            'hosts_data': hosts_data,
            'last_monitored': last_monitored,  # nightly
            'auto_tags': request.env['runbot.build.error'].disabling_tags(),
            'build_errors': request.env['runbot.build.error'].search([('random', '=', True)])
        }
        return request.render("runbot.monitoring", qctx)

    @route(['/runbot/branch/<int:branch_id>', '/runbot/branch/<int:branch_id>/page/<int:page>'], website=True, auth='public', type='http')
    def branch_builds(self, branch_id=None, search='', page=1, limit=50, refresh='', **kwargs):
        """ list builds of a runbot branch """
        domain =[('branch_id','=',branch_id), ('hidden', '=', False)]
        builds_count = request.env['runbot.build'].search_count(domain)
        pager = request.website.pager(
            url='/runbot/branch/%s' % branch_id,
            total=builds_count,
            page=page,
            step=50
        )
        builds = request.env['runbot.build'].search(domain, limit=limit, offset=pager.get('offset',0))

        context = {'pager': pager, 'builds': builds}
        return request.render("runbot.branch", context)
