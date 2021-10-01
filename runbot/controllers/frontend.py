# -*- coding: utf-8 -*-
import datetime
import werkzeug
import logging
import functools

import werkzeug.utils
import werkzeug.urls

from collections import defaultdict
from werkzeug.exceptions import NotFound, Forbidden

from odoo.addons.http_routing.models.ir_http import slug
from odoo.addons.website.controllers.main import QueryURL

from odoo.http import Controller, Response, request, route as o_route
from odoo.osv import expression

_logger = logging.getLogger(__name__)


def route(routes, **kw):
    def decorator(f):
        @o_route(routes, **kw)
        @functools.wraps(f)
        def response_wrap(*args, **kwargs):
            projects = request.env['runbot.project'].search([])
            more = request.httprequest.cookies.get('more', False) == '1'
            filter_mode = request.httprequest.cookies.get('filter_mode', 'all')
            keep_search = request.httprequest.cookies.get('keep_search', False) == '1'
            cookie_search = request.httprequest.cookies.get('search', '')
            refresh = kwargs.get('refresh', False)
            nb_build_errors = request.env['runbot.build.error'].search_count([('random', '=', True), ('parent_id', '=', False)])
            nb_assigned_errors = request.env['runbot.build.error'].search_count([('responsible', '=', request.env.user.id)])
            kwargs['more'] = more
            kwargs['projects'] = projects

            response = f(*args, **kwargs)
            if isinstance(response, Response):
                if keep_search and cookie_search and 'search' not in kwargs:
                    search = cookie_search
                else:
                    search = kwargs.get('search', '')
                if keep_search and cookie_search != search:
                    response.set_cookie('search', search)

                project = response.qcontext.get('project') or projects[0]

                response.qcontext['projects'] = projects
                response.qcontext['more'] = more
                response.qcontext['keep_search'] = keep_search
                response.qcontext['search'] = search
                response.qcontext['current_path'] = request.httprequest.full_path
                response.qcontext['refresh'] = refresh
                response.qcontext['filter_mode'] = filter_mode
                response.qcontext['qu'] = QueryURL('/runbot/%s' % (slug(project)), path_args=['search'], search=search, refresh=refresh)
                if 'title' not in response.qcontext:
                    response.qcontext['title'] = 'Runbot %s' % project.name or ''
                response.qcontext['nb_build_errors'] = nb_build_errors
                response.qcontext['nb_assigned_errors'] = nb_assigned_errors
            return response
        return response_wrap
    return decorator


class Runbot(Controller):

    def _pending(self):
        ICP = request.env['ir.config_parameter'].sudo().get_param
        warn = int(ICP('runbot.pending.warning', 5))
        crit = int(ICP('runbot.pending.critical', 12))
        pending_count = request.env['runbot.build'].search_count([('local_state', '=', 'pending'), ('build_type', '!=', 'scheduled')])
        scheduled_count = request.env['runbot.build'].search_count([('local_state', '=', 'pending'), ('build_type', '=', 'scheduled')])
        level = ['info', 'warning', 'danger'][int(pending_count > warn) + int(pending_count > crit)]
        return pending_count, level, scheduled_count

    @o_route([
        '/runbot/submit'
    ], type='http', auth="public", methods=['GET', 'POST'], csrf=False)
    def submit(self, more=False, redirect='/', keep_search=False, category=False, filter_mode=False, update_triggers=False, **kwargs):
        response = werkzeug.utils.redirect(redirect)
        response.set_cookie('more', '1' if more else '0')
        response.set_cookie('keep_search', '1' if keep_search else '0')
        response.set_cookie('filter_mode', filter_mode or 'all')
        response.set_cookie('category', category or '0')
        if update_triggers:
            enabled_triggers = []
            project_id = int(update_triggers)
            for key in kwargs.keys():
                if key.startswith('trigger_'):
                    enabled_triggers.append(key.replace('trigger_', ''))

            key = 'trigger_display_%s' % project_id
            if len(request.env['runbot.trigger'].search([('project_id', '=', project_id)])) == len(enabled_triggers):
                response.delete_cookie(key)
            else:
                response.set_cookie(key, '-'.join(enabled_triggers))
        return response

    @route(['/',
            '/runbot',
            '/runbot/<model("runbot.project"):project>',
            '/runbot/<model("runbot.project"):project>/search/<search>'], website=True, auth='public', type='http')
    def bundles(self, project=None, search='', projects=False, refresh=False, **kwargs):
        search = search if len(search) < 60 else search[:60]
        env = request.env
        categories = env['runbot.category'].search([])
        if not project and projects:
            project = projects[0]

        pending_count, level, scheduled_count = self._pending()
        context = {
            'categories': categories,
            'search': search,
            'message': request.env['ir.config_parameter'].sudo().get_param('runbot.runbot_message'),
            'pending_total': pending_count,
            'pending_level': level,
            'scheduled_count': scheduled_count,
            'hosts_data': request.env['runbot.host'].search([]),
        }
        if project:
            domain = [('last_batch', '!=', False), ('project_id', '=', project.id), ('no_build', '=', False)]

            filter_mode = request.httprequest.cookies.get('filter_mode', False)
            if filter_mode == 'sticky':
                domain.append(('sticky', '=', True))
            elif filter_mode == 'nosticky':
                domain.append(('sticky', '=', False))

            if search:
                search_domains = []
                pr_numbers = []
                for search_elem in search.split("|"):
                    if search_elem.isnumeric():
                        pr_numbers.append(int(search_elem))
                    else:
                        search_domains.append([('name', 'like', search_elem)])
                if pr_numbers:
                    res = request.env['runbot.branch'].search([('name', 'in', pr_numbers)])
                    if res:
                        search_domains.append([('id', 'in', res.mapped('bundle_id').ids)])
                search_domain = expression.OR(search_domains)
                domain = expression.AND([domain, search_domain])

            e = expression.expression(domain, request.env['runbot.bundle'])
            where_clause, where_params = e.to_sql()

            env.cr.execute("""
                SELECT id FROM runbot_bundle
                WHERE {where_clause}
                ORDER BY
                    (case when sticky then 1 when sticky is null then 2 else 2 end),
                    case when sticky then version_number end collate "C" desc,
                    last_batch desc
                LIMIT 40""".format(where_clause=where_clause), where_params)
            bundles = env['runbot.bundle'].browse([r[0] for r in env.cr.fetchall()])

            category_id = int(request.httprequest.cookies.get('category') or 0) or request.env['ir.model.data'].xmlid_to_res_id('runbot.default_category')

            trigger_display = request.httprequest.cookies.get('trigger_display_%s' % project.id, None)
            if trigger_display is not None:
                trigger_display = [int(td) for td in trigger_display.split('-') if td]
            bundles = bundles.with_context(category_id=category_id)

            triggers = env['runbot.trigger'].search([('project_id', '=', project.id)])
            context.update({
                'active_category_id': category_id,
                'bundles': bundles,
                'project': project,
                'triggers': triggers,
                'trigger_display': trigger_display,
            })

        context.update({'message': request.env['ir.config_parameter'].sudo().get_param('runbot.runbot_message')})
        res = request.render('runbot.bundles', context)
        return res

    @route([
        '/runbot/bundle/<model("runbot.bundle"):bundle>',
        '/runbot/bundle/<model("runbot.bundle"):bundle>/page/<int:page>'
        ], website=True, auth='public', type='http', sitemap=False)
    def bundle(self, bundle=None, page=1, limit=50, **kwargs):
        domain = [('bundle_id', '=', bundle.id), ('hidden', '=', False)]
        batch_count = request.env['runbot.batch'].search_count(domain)
        pager = request.website.pager(
            url='/runbot/bundle/%s' % bundle.id,
            total=batch_count,
            page=page,
            step=50,
        )
        batchs = request.env['runbot.batch'].search(domain, limit=limit, offset=pager.get('offset', 0), order='id desc')

        context = {
            'bundle': bundle,
            'batchs': batchs,
            'pager': pager,
            'project': bundle.project_id,
            'title': 'Bundle %s' % bundle.name
            }

        return request.render('runbot.bundle', context)

    @o_route([
        '/runbot/bundle/<model("runbot.bundle"):bundle>/force',
        '/runbot/bundle/<model("runbot.bundle"):bundle>/force/<int:auto_rebase>',
    ], type='http', auth="user", methods=['GET', 'POST'], csrf=False)
    def force_bundle(self, bundle, auto_rebase=False, **_post):
        _logger.info('user %s forcing bundle %s', request.env.user.name, bundle.name)  # user must be able to read bundle
        batch = bundle.sudo()._force()
        batch._log('Batch forced by %s', request.env.user.name)
        batch._prepare(auto_rebase)
        return werkzeug.utils.redirect('/runbot/batch/%s' % batch.id)

    @route(['/runbot/batch/<int:batch_id>'], website=True, auth='public', type='http', sitemap=False)
    def batch(self, batch_id=None, **kwargs):
        batch = request.env['runbot.batch'].browse(batch_id)
        context = {
            'batch': batch,
            'project': batch.bundle_id.project_id,
            'title': 'Batch %s (%s)' % (batch.id, batch.bundle_id.name)
        }
        return request.render('runbot.batch', context)

    @o_route(['/runbot/batch/slot/<model("runbot.batch.slot"):slot>/build'], auth='user', type='http')
    def slot_create_build(self, slot=None, **kwargs):
        build = slot.sudo()._create_missing_build()
        return werkzeug.utils.redirect('/runbot/build/%s' % build.id)

    @route(['/runbot/commit/<model("runbot.commit"):commit>'], website=True, auth='public', type='http', sitemap=False)
    def commit(self, commit=None, **kwargs):
        status_list = request.env['runbot.commit.status'].search([('commit_id', '=', commit.id)], order='id desc')
        last_status_by_context = dict()
        for status in status_list:
            if status.context in last_status_by_context:
                continue
            last_status_by_context[status.context] = status
        context = {
            'commit': commit,
            'project': commit.repo_id.project_id,
            'reflogs': request.env['runbot.ref.log'].search([('commit_id', '=', commit.id)]),
            'status_list': status_list,
            'last_status_by_context': last_status_by_context,
            'title': 'Commit %s' % commit.name[:8]
        }
        return request.render('runbot.commit', context)

    @o_route(['/runbot/commit/resend/<int:status_id>'], website=True, auth='user', type='http')
    def resend_status(self, status_id=None, **kwargs):
        CommitStatus = request.env['runbot.commit.status']
        status = CommitStatus.browse(status_id)
        if not status.exists():
            raise NotFound()
        last_status = CommitStatus.search([('commit_id', '=', status.commit_id.id), ('context', '=', status.context)], order='id desc', limit=1)
        if status != last_status:
            raise Forbidden("Only the last status can be resent")
        if not last_status.sent_date or (datetime.datetime.now() - last_status.sent_date).seconds > 60:  # ensure at least 60sec between two resend
            new_status = status.sudo().copy()
            new_status.description = 'Status resent by %s' % request.env.user.name
            new_status._send()
            _logger.info('github status %s resent by %s', status_id, request.env.user.name)
        return werkzeug.utils.redirect('/runbot/commit/%s' % status.commit_id.id)

    @o_route([
        '/runbot/build/<int:build_id>/<operation>',
    ], type='http', auth="public", methods=['POST'], csrf=False)
    def build_operations(self, build_id, operation, **post):
        build = request.env['runbot.build'].sudo().browse(build_id)
        if operation == 'rebuild':
            build = build._rebuild()
        elif operation == 'kill':
            build._ask_kill()
        elif operation == 'wakeup':
            build._wake_up()

        return werkzeug.utils.redirect(build.build_url)

    @route(['/runbot/build/<int:build_id>'], type='http', auth="public", website=True, sitemap=False)
    def build(self, build_id, search=None, **post):
        """Events/Logs"""

        Build = request.env['runbot.build']

        build = Build.browse([build_id])[0]
        if not build.exists():
            return request.not_found()

        context = {
            'build': build,
            'default_category': request.env['ir.model.data'].xmlid_to_res_id('runbot.default_category'),
            'project': build.params_id.trigger_id.project_id,
            'title': 'Build %s' % build.id
        }
        return request.render("runbot.build", context)

    @route([
        '/runbot/branch/<model("runbot.branch"):branch>',
        ], website=True, auth='public', type='http', sitemap=False)
    def branch(self, branch=None, **kwargs):
        pr_branch = branch.bundle_id.branch_ids.filtered(lambda rec: not rec.is_pr and rec.id != branch.id and rec.remote_id.repo_id == branch.remote_id.repo_id)[:1]
        branch_pr = branch.bundle_id.branch_ids.filtered(lambda rec: rec.is_pr and rec.id != branch.id and rec.remote_id.repo_id == branch.remote_id.repo_id)[:1]
        context = {
            'branch': branch,
            'project': branch.remote_id.repo_id.project_id,
            'title': 'Branch %s' % branch.name,
            'pr_branch': pr_branch,
            'branch_pr': branch_pr
            }

        return request.render('runbot.branch', context)

    @route([
        '/runbot/glances',
        '/runbot/glances/<int:project_id>'
        ], type='http', auth='public', website=True, sitemap=False)
    def glances(self, project_id=None, **kwargs):
        project_ids = [project_id] if project_id else request.env['runbot.project'].search([]).ids # search for access rights
        bundles = request.env['runbot.bundle'].search([('sticky', '=', True), ('project_id', 'in', project_ids)])
        pending = self._pending()
        qctx = {
            'pending_total': pending[0],
            'pending_level': pending[1],
            'bundles': bundles,
            'title': 'Glances'
        }
        return request.render("runbot.glances", qctx)

    @route(['/runbot/monitoring',
            '/runbot/monitoring/<int:category_id>',
            '/runbot/monitoring/<int:category_id>/<int:view_id>'], type='http', auth='user', website=True, sitemap=False)
    def monitoring(self, category_id=None, view_id=None, **kwargs):
        pending = self._pending()
        hosts_data = request.env['runbot.host'].search([])
        if category_id:
            category = request.env['runbot.category'].browse(category_id)
            assert category.exists()
        else:
            category = request.env.ref('runbot.nightly_category')
            category_id = category.id
        bundles = request.env['runbot.bundle'].search([('sticky', '=', True)])  # NOTE we dont filter on project
        qctx = {
            'category': category,
            'pending_total': pending[0],
            'pending_level': pending[1],
            'scheduled_count': pending[2],
            'bundles': bundles,
            'hosts_data': hosts_data,
            'auto_tags': request.env['runbot.build.error'].disabling_tags(),
            'build_errors': request.env['runbot.build.error'].search([('random', '=', True)]),
            'kwargs': kwargs,
            'title': 'monitoring'
        }
        return request.render(view_id if view_id else "runbot.monitoring", qctx)

    @route(['/runbot/errors',
            '/runbot/errors/page/<int:page>'
            ], type='http', auth='user', website=True, sitemap=False)
    def build_errors(self, sort=None, page=1, limit=20, **kwargs):
        sort_order_choices = {
            'last_seen_date desc': 'Last seen date: Newer First',
            'last_seen_date asc': 'Last seen date: Older First',
            'build_count desc': 'Number seen: High to Low',
            'build_count asc': 'Number seen: Low to High',
            'responsible asc': 'Assignee: A - Z',
            'responsible desc': 'Assignee: Z - A',
            'module_name asc': 'Module name: A - Z',
            'module_name desc': 'Module name: Z -A'
        }

        sort_order = sort if sort in sort_order_choices else 'last_seen_date desc'

        current_user_errors = request.env['runbot.build.error'].search([
            ('responsible', '=', request.env.user.id),
            ('parent_id', '=', False),
        ], order='last_seen_date desc, build_count desc')

        domain = [('parent_id', '=', False), ('responsible', '!=', request.env.user.id), ('build_count', '>', 1)]
        build_errors_count = request.env['runbot.build.error'].search_count(domain)
        url_args = {}
        url_args['sort'] = sort
        pager = request.website.pager(url='/runbot/errors/', url_args=url_args, total=build_errors_count, page=page, step=limit)

        build_errors = request.env['runbot.build.error'].search(domain, order=sort_order, limit=limit, offset=pager.get('offset', 0))

        qctx = {
            'current_user_errors': current_user_errors,
            'build_errors': build_errors,
            'title': 'Build Errors',
            'sort_order_choices': sort_order_choices,
            'pager': pager
        }
        return request.render('runbot.build_error', qctx)

    @route(['/runbot/teams', '/runbot/teams/<model("runbot.team"):team>',], type='http', auth='user', website=True, sitemap=False)
    def team_dashboards(self, team=None, hide_empty=False, **kwargs):
        teams = request.env['runbot.team'].search([]) if not team else None
        qctx = {
            'team': team,
            'teams': teams,
            'hide_empty': bool(hide_empty),
        }
        return request.render('runbot.team', qctx)

    @route(['/runbot/dashboards/<model("runbot.dashboard"):dashboard>',], type='http', auth='user', website=True, sitemap=False)
    def dashboards(self, dashboard=None, hide_empty=False, **kwargs):
        qctx = {
            'dashboard': dashboard,
            'hide_empty': bool(hide_empty),
        }
        return request.render('runbot.dashboard_page', qctx)

    @route(['/runbot/build/stats/<int:build_id>'], type='http', auth="public", website=True, sitemap=False)
    def build_stats(self, build_id, search=None, **post):
        """Build statistics"""

        Build = request.env['runbot.build']

        build = Build.browse([build_id])[0]
        if not build.exists():
            return request.not_found()

        build_stats = defaultdict(dict)
        for stat in build.stat_ids.filtered(lambda rec: '.' in rec.key).sorted(key=lambda  rec: rec.value, reverse=True):
            category, module = stat.key.split('.', maxsplit=1)
            value = int(stat.value) if stat.value == int(stat.value) else stat.value
            build_stats[category].update({module: value})

        context = {
            'build': build,
            'build_stats': build_stats,
            'default_category': request.env['ir.model.data'].xmlid_to_res_id('runbot.default_category'),
            'project': build.params_id.trigger_id.project_id,
            'title': 'Build %s statistics' % build.id
        }
        return request.render("runbot.build_stats", context)


    @route(['/runbot/stats/'], type='json', auth="public", website=False, sitemap=False)
    def stats_json(self, bundle_id=False, trigger_id=False, key_category='', center_build_id=False, limit=100, search=None, **post):
        """ Json stats """
        trigger_id = trigger_id and int(trigger_id)
        bundle_id = bundle_id and int(bundle_id)
        center_build_id = center_build_id and int(center_build_id)
        limit = min(int(limit), 1000)

        trigger = request.env['runbot.trigger'].browse(trigger_id)
        bundle = request.env['runbot.bundle'].browse(bundle_id)
        if not trigger_id or not bundle_id or not trigger.exists() or not bundle.exists():
            return request.not_found()

        builds_domain = [
            ('build_type', '=', 'normal'),
            ('global_state', 'in', ('running', 'done')),
            ('global_result', '=', 'ok'),
            ('slot_ids.batch_id.bundle_id', '=', bundle_id),
            ('params_id.trigger_id', '=', trigger.id),
        ]
        builds = request.env['runbot.build']
        if center_build_id:
            builds = builds.search(
                expression.AND([builds_domain, [('id', '>=', center_build_id)]]), 
                order='id', limit=limit/2)
            builds_domain = expression.AND([builds_domain, [('id', '<=', center_build_id)]])
            limit -= len(builds)

        builds |= builds.search(builds_domain, order='id desc', limit=limit)

        builds = builds.search([('id', 'child_of', builds.ids)])

        parents = {b.id: b.top_parent.id for b in builds.with_context(prefetch_fields=False)}
        request.env.cr.execute("SELECT build_id, key, value FROM runbot_build_stat WHERE build_id IN %s AND key like %s", [tuple(builds.ids), '%s.%%' % key_category]) # read manually is way faster than using orm
        res = {}
        for (builds_id, key, value) in request.env.cr.fetchall():
            res.setdefault(parents[builds_id], {})[key.split('.', 1)[1]] = value
        return res

    @route(['/runbot/stats/<model("runbot.bundle"):bundle>/<model("runbot.trigger"):trigger>'], type='http', auth="public", website=True, sitemap=False)
    def modules_stats(self, bundle, trigger, search=None, **post):
        """Modules statistics"""

        categories = request.env['runbot.build.stat.regex'].search([]).mapped('name')

        context = {
            'stats_categories': categories,
            'bundle': bundle,
            'trigger': trigger,
        }

        return request.render("runbot.modules_stats", context)
