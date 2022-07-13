import json

from functools import wraps
from odoo.exceptions import AccessError
from odoo.http import Controller, request, route
from odoo.osv import expression

RECORDS_PER_PAGE = 100

def to_json(fn):
    @wraps(fn)
    def decorator(*args, **kwargs):
        headers = [('Content-Type', 'application/json'),
                ('Cache-Control', 'no-store')]
        try:
            return request.make_response(json.dumps(fn(*args, **kwargs)._get_description(), indent=4, default=str), headers)
        except AccessError:
            response = request.make_response(json.dumps('unauthorized'), headers)
            response.status = 403
            return response
    return decorator

class RunbotJsonRoutes(Controller):

    @route(['/runbot/json/projects',
            '/runbot/json/projects/<int:project_id>'], type='http', auth='public')
    @to_json
    def projects(self, project_id=None, **kwargs):
        if project_id:
            projects = request.env['runbot.project'].browse(project_id)
        else:
            domain = ([('group_ids', '=', False)]) if request.env.user._is_public() else []
            projects = request.env['runbot.project'].search(domain)
        return projects

    @route(['/runbot/json/bundles/<int:bundle_id>',
            '/runbot/json/projects/<int:project_id>/bundles'], type='http', auth='public')
    @to_json
    def bundles(self, project_id=None, bundle_id=None, page=0, **kwargs):
        offset = int(page) * RECORDS_PER_PAGE
        domain = []
        if bundle_id:
            bundles = request.env['runbot.bundle'].browse(bundle_id)
        else:
            domain = [('project_id', '=', project_id)]
            if 'sticky' in kwargs:
                domain = expression.AND([domain, [('sticky', '=', kwargs['sticky'])]])
            if 'name' in kwargs:
                name_query = kwargs['name']
                name_query = name_query if len(name_query) < 60 else name_query[:60]
                domain = expression.AND([domain, [('name', 'ilike', name_query)]])
            bundles = request.env['runbot.bundle'].search(domain, order='id desc', limit=RECORDS_PER_PAGE, offset=offset)
        return bundles

    @route(['/runbot/json/bundles/<int:bundle_id>/batches',
            '/runbot/json/batches/<int:batch_id>'], type='http', auth='public')
    @to_json
    def batches(self, bundle_id=None, batch_id=None, page=0, **kwargs):
        offset = int(page) * RECORDS_PER_PAGE
        if batch_id:
            batches = request.env['runbot.batch'].browse(batch_id)
        else:
            domain = [('bundle_id', '=', bundle_id)] if bundle_id else []
            domain += [('state', '=', kwargs['state'])] if 'state' in kwargs else []
            batches = request.env['runbot.batch'].search(domain, order="id desc", limit=RECORDS_PER_PAGE, offset=offset)
        return batches

    @route(['/runbot/json/batches/<int:batch_id>/commits',
            '/runbot/json/commits/<int:commit_id>'], type='http', auth='public')
    @to_json
    def commits(self, commit_id=None, batch_id=None, page=0, **kwargs):
        if commit_id:
            commits = request.env['runbot.commit'].browse(commit_id)
        else:
            commits = request.env['runbot.batch'].browse(batch_id).commit_ids
        return commits

    @route(['/runbot/json/commits/<int:commit_id>/commit_links',
            '/runbot/json/commit_links/<int:commit_link_id>'], type='http', auth='public')
    @to_json
    def commit_links(self, commit_id=None, commit_link_id=None, page=0, **kwargs):
        if commit_link_id:
            commit_links = request.env['runbot.commit.link'].browse(commit_link_id)
        else:
            domain = [('commit_id', '=', commit_id)]
            commit_links = request.env['runbot.commit.link'].search(domain)
        return commit_links

    @route(['/runbot/json/repos',
            '/runbot/json/repos/<int:repo_id>'], type='http', auth='public')
    @to_json
    def repos(self, repo_id=None, page=0, **kwargs):
        if repo_id:
            repos = request.env['runbot.repo'].browse(repo_id)
        else:
            domain = []
            repos = request.env['runbot.repo'].search(domain)
        return repos

    @route(['/runbot/json/batches/<int:batch_id>/slots',
            '/runbot/json/batch_slots/<int:slot_id>'], type='http', auth='public')
    @to_json
    def batch_slots(self, batch_id=None, slot_id=None, page=0, **kwargs):
        if slot_id:
            slots = request.env['runbot.batch.slot'].browse(slot_id)
        else:
            slots = request.env['runbot.batch'].browse(batch_id).slot_ids
        return slots

    @route(['/runbot/json/batches/<int:batch_id>/builds',
            '/runbot/json/builds/<int:build_id>'], type='http', auth='public')
    @to_json
    def builds(self, build_id=None, batch_id=None, page=0, **kwargs):
        if build_id:
            builds = request.env['runbot.build'].browse(build_id)
        else:
            builds = request.env['runbot.batch'].browse(batch_id).all_build_ids
        return builds
