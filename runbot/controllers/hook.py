# -*- coding: utf-8 -*-

import time
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class Hook(http.Controller):

    @http.route(['/runbot/hook', '/runbot/hook/<int:remote_id>'], type='http', auth="public", website=True, csrf=False)
    def hook(self, remote_id=None, **_post):
        event = request.httprequest.headers.get("X-Github-Event")
        payload = json.loads(request.params.get('payload', '{}'))
        if remote_id is None:
            repo_data = payload.get('repository')
            if repo_data:
                remote_domain = [
                    '|', '|', '|',
                    ('name', '=', repo_data['ssh_url']),
                    ('name', '=', repo_data['ssh_url'].replace('.git', '')),
                    ('name', '=', repo_data['clone_url']),
                    ('name', '=', repo_data['clone_url'].replace('.git', '')),
                ]
                remote = request.env['runbot.remote'].sudo().search(
                    remote_domain, limit=1)
                remote_id = remote.id
                if not remote_id:
                    _logger.error("Remote %s not found", repo_data['ssh_url'])
        remote = request.env['runbot.remote'].sudo().browse(remote_id)
        _logger.info('Remote found %s', remote)

        # force update of dependencies too in case a hook is lost
        if not payload or event == 'push':
            remote.repo_id.set_hook_time(time.time())
        elif event == 'pull_request':
            pr_number = payload.get('pull_request', {}).get('number', '')
            branch = request.env['runbot.branch'].sudo().search([('remote_id', '=', remote.id), ('name', '=', pr_number)])
            branch.recompute_infos(payload.get('pull_request', {}))
            if payload.get('action') in ('synchronize', 'opened', 'reopened'):
                remote.repo_id.set_hook_time(time.time())
            # remaining recurrent actions: labeled, review_requested, review_request_removed
        elif event == 'delete':
            if payload.get('ref_type') == 'branch':
                branch_ref = payload.get('ref')
                _logger.info('Branch %s in repo %s was deleted', branch_ref, remote.repo_id.name)
                branch = request.env['runbot.branch'].sudo().search([('remote_id', '=', remote.id), ('name', '=', branch_ref)])
                branch.alive = False
        return ""
