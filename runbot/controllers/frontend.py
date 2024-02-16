# -*- coding: utf-8 -*-
import datetime
import werkzeug
import logging
import functools

import werkzeug.utils
import werkzeug.urls

from collections import defaultdict, OrderedDict
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
            nb_team_errors = request.env['runbot.build.error'].search_count([('responsible', '=', False), ('team_id', 'in', request.env.user.runbot_team_ids.ids)])
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

                project = response.qcontext.get('project') or projects and projects[0]

                response.qcontext['projects'] = projects
                response.qcontext['more'] = more
                response.qcontext['keep_search'] = keep_search
                response.qcontext['search'] = search
                response.qcontext['current_path'] = request.httprequest.full_path
                response.qcontext['refresh'] = refresh
                response.qcontext['filter_mode'] = filter_mode
                response.qcontext['default_category'] = request.env['ir.model.data']._xmlid_to_res_id('runbot.default_category')

                response.qcontext['qu'] = QueryURL('/runbot/%s' % (slug(project) if project else ''), path_args=['search'], search=search, refresh=refresh)
                if 'title' not in response.qcontext:
                    response.qcontext['title'] = 'Runbot %s' % project.name or ''
                response.qcontext['nb_build_errors'] = nb_build_errors
                response.qcontext['nb_assigned_errors'] = nb_assigned_errors
                response.qcontext['nb_team_errors'] = nb_team_errors
            return response
        return response_wrap
    return decorator


class Runbot(Controller):

    def _pending(self):
        ICP = request.env['ir.config_parameter'].sudo().get_param
        warn = int(ICP('runbot.pending.warning', 5))
        crit = int(ICP('runbot.pending.critical', 12))
        pending_count = request.env['runbot.build'].search_count([('local_state', '=', 'pending'), ('build_type', '!=', 'scheduled'), ('host', '=', False)])
        scheduled_count = request.env['runbot.build'].search_count([('local_state', '=', 'pending'), ('build_type', '=', 'scheduled')])
        pending_assigned_count = request.env['runbot.build'].search_count([('local_state', '=', 'pending'), ('build_type', '!=', 'scheduled'), ('host', '!=', False)])
        level = ['info', 'warning', 'danger'][int(pending_count > warn) + int(pending_count > crit)]
        return pending_count, level, scheduled_count, pending_assigned_count

    @o_route([
        '/runbot/submit'
    ], type='http', auth="public", methods=['GET', 'POST'], csrf=False)
    def submit(self, more=False, redirect='/', keep_search=False, category=False, filter_mode=False, update_triggers=False, **kwargs):
        assert redirect.startswith('/runbot/')
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
    def bundles(self, project=None, search='', projects=False, refresh=False, for_next_freeze=False, limit=40, **kwargs):
        search = search if len(search) < 60 else search[:60]
        env = request.env
        categories = env['runbot.category'].search([])
        if not project and projects:
            project = projects[0]

        pending_count, level, scheduled_count, pending_assigned_count = self._pending()
        context = {
            'categories': categories,
            'search': search,
            'message': request.env['ir.config_parameter'].sudo().get_param('runbot.runbot_message'),
            'pending_count': pending_count,
            'pending_assigned_count': pending_assigned_count,
            'pending_level': level,
            'scheduled_count': scheduled_count,
            'hosts_data': request.env['runbot.host'].search([('assigned_only', '=', False)]),
        }
        if project:
            domain = [('last_batch', '!=', False), ('project_id', '=', project.id), ('no_build', '=', False)]

            filter_mode = request.httprequest.cookies.get('filter_mode', False)
            if filter_mode == 'sticky':
                domain.append(('sticky', '=', True))
            elif filter_mode == 'nosticky':
                domain.append(('sticky', '=', False))

            if for_next_freeze:
                domain.append(('for_next_freeze', '=', True))

            if search:
                search_domains = []
                pr_numbers = []
                for search_elem in search.split("|"):
                    if search_elem.isnumeric():
                        pr_numbers.append(int(search_elem))
                    operator = '=ilike' if '%' in search_elem else 'ilike'
                    search_domains.append([('name', operator, search_elem)])
                if pr_numbers:
                    res = request.env['runbot.branch'].search([('name', 'in', pr_numbers)])
                    if res:
                        search_domains.append([('id', 'in', res.mapped('bundle_id').ids)])
                search_domain = expression.OR(search_domains)
                domain = expression.AND([domain, search_domain])

            e = expression.expression(domain, request.env['runbot.bundle'])
            query = e.query
            query.order = """
             (case when "runbot_bundle".sticky then 1 when "runbot_bundle".sticky is null then 2 else 2 end),
                    case when "runbot_bundle".sticky then "runbot_bundle".version_number end collate "C" desc,
                    "runbot_bundle".last_batch desc
            """
            query.limit = min(limit, 200)
            bundles = env['runbot.bundle'].browse(query)

            category_id = int(request.httprequest.cookies.get('category') or 0) or request.env['ir.model.data']._xmlid_to_res_id('runbot.default_category')

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
        if not request.env.user.has_group('runbot.group_runbot_advanced_user'):
            raise Forbidden("Only users with a specific group can do that. Please contact runbot administrators")
        _logger.info('user %s forcing bundle %s', request.env.user.name, bundle.name)  # user must be able to read bundle
        batch = bundle.sudo()._force()
        if batch:
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

    @route([
        '/runbot/commit/<model("runbot.commit"):commit>',
        '/runbot/commit/<string(minlength=6, maxlength=40):commit_hash>'
    ], website=True, auth='public', type='http', sitemap=False)
    def commit(self, commit=None, commit_hash=None, **kwargs):
        if commit_hash:
            commit = request.env['runbot.commit'].search([('name', '=like', f'{commit_hash}%')], limit=1)
            if not commit.exists():
                raise NotFound()
            return request.redirect(f"/runbot/commit/{slug(commit)}")
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
    ], type='http', auth="user", methods=['POST'], csrf=False)
    def build_operations(self, build_id, operation, **post):
        build = request.env['runbot.build'].sudo().browse(build_id)
        if operation == 'rebuild':
            build = build._rebuild()
        elif operation == 'kill':
            build._ask_kill()
        elif operation == 'wakeup':
            build._wake_up()

        return str(build.id)

    @route([
        '/runbot/build/<int:build_id>',
        '/runbot/batch/<int:from_batch>/build/<int:build_id>'
    ], type='http', auth="public", website=True, sitemap=False)
    def build(self, build_id, search=None, from_batch=None, **post):
        """Events/Logs"""

        if from_batch:
            from_batch = request.env['runbot.batch'].browse(int(from_batch))
            if build_id not in from_batch.with_context(active_test=False).slot_ids.build_id.ids:
                # the url may have been forged replacing the build id, redirect to hide the batch
                return werkzeug.utils.redirect('/runbot/build/%s' % build_id)

            from_batch = from_batch.with_context(batch=from_batch)
        Build = request.env['runbot.build'].with_context(batch=from_batch)

        build = Build.browse([build_id])[0]
        if not build.exists():
            return request.not_found()
        siblings = (build.parent_id.children_ids if build.parent_id else from_batch.slot_ids.build_id if from_batch else build).sorted('id')
        context = {
            'build': build,
            'from_batch': from_batch,
            'project': build.params_id.trigger_id.project_id,
            'title': 'Build %s' % build.id,
            'siblings': siblings,
            # following logic is not the most efficient but good enough
            'prev_ko': next((b for b in reversed(siblings) if b.id < build.id and b.global_result != 'ok'), Build),
            'prev_bu': next((b for b in reversed(siblings) if b.id < build.id), Build),
            'next_bu': next((b for b in siblings if b.id > build.id), Build),
            'next_ko': next((b for b in siblings if b.id > build.id and b.global_result != 'ok'), Build),
        }
        return request.render("runbot.build", context)

    @route([
    '/runbot/build/search',
    ], website=True, auth='public', type='http', sitemap=False)
    def builds(self, **kwargs):
        domain = []
        for key in ('config_id', 'version_id', 'project_id', 'trigger_id', 'create_batch_id.bundle_id', 'create_batch_id'): # allowed params
            value = kwargs.get(key)
            if value:
                domain.append((f'params_id.{key}', '=', int(value)))

        for key in ('global_state', 'local_state', 'global_result', 'local_result'):
            value = kwargs.get(key)
            if value:
                domain.append((f'{key}', '=', value))

        for key in ('description',):
            if key in kwargs:
                domain.append((f'{key}', 'ilike', kwargs.get(key)))

        context = {
            'builds': request.env['runbot.build'].search(domain, limit=100),
        }

        return request.render('runbot.build_search', context)

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
        pending_count, level, scheduled_count, pending_assigned_count = self._pending()
        qctx = {
            'pending_count': pending_count,
            'pending_assigned_count': pending_assigned_count,
            'pending_level': level,
            'bundles': bundles,
            'title': 'Glances'
        }
        return request.render("runbot.glances", qctx)

    @route(['/runbot/monitoring',
            '/runbot/monitoring/<int:category_id>',
            '/runbot/monitoring/<int:category_id>/<int:view_id>'], type='http', auth='user', website=True, sitemap=False)
    def monitoring(self, category_id=None, view_id=None, **kwargs):
        pending_count, level, scheduled_count, pending_assigned_count = self._pending()
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
            'pending_count': pending_count,
            'pending_assigned_count': pending_assigned_count,
            'pending_level': level,
            'scheduled_count': scheduled_count,
            'bundles': bundles,
            'hosts_data': hosts_data,
            'auto_tags': request.env['runbot.build.error']._disabling_tags(),
            'build_errors': request.env['runbot.build.error'].search([('random', '=', True)]),
            'kwargs': kwargs,
            'title': 'monitoring'
        }
        return request.render(view_id if view_id else "runbot.monitoring", qctx)

    @route(['/runbot/errors/assign/<int:build_error_id>'
            ], type='http', auth='user', methods=['POST'], csrf=False, sitemap=False)
    def build_errors_assign(self, build_error_id=None, **kwargs):
        build_error = request.env['runbot.build.error'].browse(build_error_id)
        if build_error.responsible:
            return build_error.responsible.name
        if request.env.user._is_internal():
            build_error.sudo().responsible = request.env.user
            return request.env.user.name
        return 'Error'

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
        ], order='last_seen_date desc, build_count desc')
        current_team_errors = request.env['runbot.build.error'].search([
            ('responsible', '=', False),
            ('team_id', 'in', request.env.user.runbot_team_ids.ids)
        ], order='last_seen_date desc, build_count desc')
        domain = [('parent_id', '=', False), ('responsible', '!=', request.env.user.id), ('build_count', '>', 1)]
        build_errors_count = request.env['runbot.build.error'].search_count(domain)
        url_args = {}
        url_args['sort'] = sort
        pager = request.website.pager(url='/runbot/errors/', url_args=url_args, total=build_errors_count, page=page, step=limit)

        build_errors = request.env['runbot.build.error'].search(domain, order=sort_order, limit=limit, offset=pager.get('offset', 0))

        qctx = {
            'current_user_errors': current_user_errors,
            'current_team_errors': current_team_errors,
            'build_errors': build_errors,
            'title': 'Build Errors',
            'sort_order_choices': sort_order_choices,
            'page': page,
            'pager': pager,
        }
        return request.render('runbot.build_error', qctx)

    @route(['/runbot/teams', '/runbot/teams/<model("runbot.team"):team>',], type='http', auth='user', website=True, sitemap=False)
    def team_dashboards(self, team=None, hide_empty=False, **kwargs):
        teams = request.env['runbot.team'].search([]) if not team else None
        domain = [('id', 'in', team.build_error_ids.ids)] if team else []

        # Sort & Filter
        sortby = kwargs.get('sortby', 'count')
        searchbar_sortings = {
            'date': {'label': 'Recently Seen', 'order': 'last_seen_date desc'},
            'count': {'label': 'Nb Seen', 'order': 'build_count desc'},
        }
        order = searchbar_sortings[sortby]['order']
        searchbar_filters = {
            'all': {'label': 'All', 'domain': []},
            'unassigned': {'label': 'Unassigned', 'domain': [('responsible', '=', False)]},
            'not_one': {'label': 'Seen more than once', 'domain': [('build_count', '>', 1)]},
        }

        for trigger in team.build_error_ids.trigger_ids if team else []:
            k = f'trigger_{trigger.name.lower().replace(" ", "_")}'
            searchbar_filters.update(
                {k: {'label': f'Trigger {trigger.name}', 'domain': [('trigger_ids', '=', trigger.id)]}}
            )

        filterby = kwargs.get('filterby', 'not_one')
        if filterby not in searchbar_filters:
            filterby = 'not_one'
        domain = expression.AND([domain, searchbar_filters[filterby]['domain']])

        qctx = {
            'team': team,
            'teams': teams,
            'build_error_ids': request.env['runbot.build.error'].search(domain, order=order),
            'hide_empty': bool(hide_empty),
            'searchbar_sortings': searchbar_sortings,
            'sortby': sortby,
            'searchbar_filters': OrderedDict(sorted(searchbar_filters.items())),
            'filterby': filterby,
            'default_url': request.httprequest.path,
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
        for stat in build.stat_ids:
            for module, value in sorted(stat.values.items(), key=lambda item: item[1], reverse=True):
                build_stats[stat.category][module] = value

        context = {
            'build': build,
            'build_stats': build_stats,
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
            ('global_state', 'in', ('running', 'done')),
            ('slot_ids.batch_id.bundle_id', '=', bundle_id),
            ('params_id.trigger_id', '=', trigger.id),
        ]
        builds = request.env['runbot.build'].with_context(active_test=False)
        if center_build_id:
            builds = builds.search(
                expression.AND([builds_domain, [('id', '>=', center_build_id)]]), 
                order='id', limit=limit/2)
            builds_domain = expression.AND([builds_domain, [('id', '<=', center_build_id)]])
            limit -= len(builds)

        builds |= builds.search(builds_domain, order='id desc', limit=limit)
        if not builds:
            return {}

        builds = builds.search([('id', 'child_of', builds.ids)])

        parents = {b.id: b.top_parent.id for b in builds.with_context(prefetch_fields=False)}
        request.env.cr.execute("SELECT build_id, values FROM runbot_build_stat WHERE build_id IN %s AND category = %s", [tuple(builds.ids), key_category]) # read manually is way faster than using orm
        res = {}
        for (build_id, values) in request.env.cr.fetchall():
            if values:
                res.setdefault(parents[build_id], {}).update(values)
            # we need to update here to manage the post install case: we want to combine stats from all post_install childrens.
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

    @route(['/runbot/load_info'], type='http', auth="user", website=True, sitemap=False)
    def load_infos(self, **post):
        build_by_bundle = {}

        for build in request.env['runbot.build'].search([('local_state', 'in', ('pending', 'testing'))], order='id'):
            build_by_bundle.setdefault(build.params_id.create_batch_id.bundle_id, []).append(build)

        build_by_bundle = list(build_by_bundle.items())
        build_by_bundle.sort(key=lambda x: -len(x[1]))
        pending_count, level, scheduled_count, pending_assigned_count = self._pending()
        context = {
            'build_by_bundle': build_by_bundle,
            'pending_count': pending_count,
            'pending_assigned_count': pending_assigned_count,
            'pending_level': level,
            'scheduled_count': scheduled_count,
            'hosts_data': request.env['runbot.host'].search([('assigned_only', '=', False)]),
        }

        return request.render("runbot.load_info", context)

    @route([
        '/runbot/run/<build_id>',
        '/runbot/run/<build_id>/<db_suffix>',
    ], type='http', auth="public", website=True, sitemap=False)
    def access_running(self, build_id, db_suffix=None, **kwargs):
        build = request.env['runbot.build'].browse(int(build_id)).exists()
        run_url = build._get_run_url(db_suffix)
        _logger.info('Redirecting to %s', run_url)
        return werkzeug.utils.redirect(run_url)

    @route(['/runbot/parse_log/<model("ir.logging"):ir_log>'], type='http', auth='user', sitemap=False)
    def parse_log(self, ir_log, **kwargs):
        request.env['runbot.build.error']._parse_logs(ir_log)
        return werkzeug.utils.redirect('/runbot/build/%s' % ir_log.build_id.id)
