# -*- coding: utf-8 -*-

import datetime
import json

from odoo import http, tools
from odoo.http import request


class RunbotHook(http.Controller):

    @http.route(['/runbot/hook/<int:repo_id>', '/runbot/hook/org'], type='http', auth="public", website=True, csrf=False)
    def hook(self, repo_id=None, **post):
        if repo_id is None:
            event = request.httprequest.headers.get("X-Github-Event")
            repo_data = json.loads(request.params['payload']).get('repository')
            if repo_data and event in ['push', 'pull_request']:
                repo_domain = [
                    '|', '|', ('name', '=', repo_data['ssh_url']),
                    ('name', '=', repo_data['clone_url']),
                    ('name', '=', repo_data['clone_url'].rstrip('.git')),
                ]
                repo = request.env['runbot.repo'].sudo().search(
                    repo_domain, limit=1)
                repo_id = repo.id

        repo = request.env['runbot.repo'].sudo().browse([repo_id])
        repo.hook_time = datetime.datetime.now().strftime(tools.DEFAULT_SERVER_DATETIME_FORMAT)
        return ""
