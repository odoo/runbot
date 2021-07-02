# -*- coding: utf-8 -*-

import time
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class Hook(http.Controller):

    @http.route(['/runbot/hook/<int:remote_id>', '/runbot/hook/org'], type='http', auth="public", website=True, csrf=False)
    def hook(self, remote_id=None, **post):
        _logger.info('Hook on %s', remote_id)
        event = request.httprequest.headers.get("X-Github-Event")
        payload = json.loads(request.params.get('payload', '{}'))
        if remote_id is None:
            repo_data = payload.get('repository')
            if repo_data and event in ['push', 'pull_request']:
                remote_domain = [
                    '|', '|', ('name', '=', repo_data['ssh_url']),
                    ('name', '=', repo_data['clone_url']),
                    ('name', '=', repo_data['clone_url'].rstrip('.git')),
                ]
                remote = request.env['runbot.remote'].sudo().search(
                    remote_domain, limit=1)
                remote_id = remote.id

        remote = request.env['runbot.remote'].sudo().browse([remote_id])

        # force update of dependencies too in case a hook is lost
        if not payload or event == 'push' or (event == 'pull_request' and payload.get('action') in ('synchronize', 'opened', 'reopened')):
            remote.repo_id.set_hook_time(time.time())
        elif event == 'pull_request':
            pr_number = payload.get('pull_request', {}).get('number', '')
            branch = request.env['runbot.branch'].sudo().search([('remote_id', '=', remote.id), ('name', '=', pr_number)])
            if payload and payload.get('action', '') == 'edited' and 'base' in payload.get('changes'):
                # handle PR that have been re-targeted
                branch._compute_branch_infos(payload.get('pull_request', {}))
                _logger.info('Retargeting %s to %s', branch.name, branch.target_branch_name)
                base = request.env['runbot.bundle'].search([
                    ('name', '=', branch.target_branch_name),
                    ('is_base', '=', True),
                    ('project_id', '=', branch.remote_id.repo_id.project_id.id)
                ])
                if base and branch.bundle_id.defined_base_id != base:
                    _logger.info('Changing base of bundle %s to %s(%s)', branch.bundle_id, base.name, base.id)
                    branch.bundle_id.defined_base_id = base.id
                    branch.bundle_id._force()
            elif payload.get('action') in ('ready_for_review', 'converted_to_draft'):
                init_draft = branch.draft
                branch._compute_branch_infos(payload.get('pull_request', {}))
                if branch.draft != init_draft:
                    branch.bundle_id._force()
            elif payload.get('action') in ('deleted', 'closed'):
                _logger.info('Closing pr %s', branch.name)
                branch.alive = False
            else:
                _logger.info('Ignoring unsupported pull request operation %s %s', event, payload.get('action', ''))
        elif event == 'delete':
            if payload.get('ref_type') == 'branch':
                branch_ref = payload.get('ref')
                _logger.info('Hook for branch deletion %s in repo %s', branch_ref, remote.repo_id.name)
                branch = request.env['runbot.branch'].sudo().search([('remote_id', '=', remote.id), ('name', '=', branch_ref)])
                branch.alive = False
        else:
            _logger.info('Ignoring unsupported hook %s %s', event, payload.get('action', ''))
        return ""
