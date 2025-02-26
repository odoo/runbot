# -*- coding: utf-8 -*-

import json
import time
import logging
import hashlib
import hmac

from werkzeug.exceptions import BadRequest

from odoo import http, fields
from odoo.http import request
from ..common import from_role

_logger = logging.getLogger(__name__)


def verify_signature(remote):
    req = request.httprequest
    webhook_secret = remote.webhook_secret or remote.repo_id.project_id.webhook_secret
    signature_header = req.headers.get('X-Hub-Signature-256', '')
    if not webhook_secret:  # TODO enforce webhook_secret if signature_header is given
        if signature_header:
            _logger.warning('No webhook_secret on remote %s while one is configured on webhook', remote.name)
        return
    if not signature_header:
        _logger.warning('Received payload without signature header')
        raise BadRequest(description="x-hub-signature-256 header is missing!")
    signature = 'sha256=' + hmac.new(webhook_secret.strip().encode(), req.get_data(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, signature_header):
        _logger.info('Received payload with invalid signature for remote %s: %s', remote.name, signature_header)
        raise BadRequest(description="Request signatures didn't match!")


class Hook(http.Controller):

    @http.route(['/runbot/hook', '/runbot/hook/<int:remote_id>'], type='http', auth="public", website=True, csrf=False, sitemap=False)
    def hook(self, remote_id=None, **_post):
        event = request.httprequest.headers.get("X-Github-Event")
        if request.httprequest.mimetype == 'application/json':
            payload = request.get_json_data() if request.httprequest.data else {}
        else:
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
        remote = request.env['runbot.remote'].sudo().browse(remote_id).exists()
        if not remote:
            raise BadRequest(description='Invalid remote')
        verify_signature(remote)

        # force update of dependencies too in case a hook is lost
        if not payload or event == 'push':
            remote.repo_id._set_hook_time(time.time())
        elif event == 'pull_request':
            pr_number = payload.get('pull_request', {}).get('number', '')
            branch = request.env['runbot.branch'].sudo().search([('remote_id', '=', remote.id), ('name', '=', pr_number)])
            branch._recompute_infos(payload.get('pull_request', {}))
            if payload.get('action') in ('synchronize', 'opened', 'reopened'):
                remote.repo_id._set_hook_time(time.time())
            # remaining recurrent actions: labeled, review_requested, review_request_removed
        elif event == 'delete':
            if payload.get('ref_type') == 'branch':
                branch_ref = payload.get('ref')
                _logger.info('Branch %s in repo %s was deleted', branch_ref, remote.repo_id.name)
                branch = request.env['runbot.branch'].sudo().search([('remote_id', '=', remote.id), ('name', '=', branch_ref)])
                branch.alive = False
        return ""

    @from_role('mergebot', signed=True)
    @http.route(['/runbot/request_ci'], type='http', methods=["POST"], auth="public", website=True, csrf=False, sitemap=False)
    def force_ci(self):
        pull_request_names = request.get_json_data().get('pull_requests', [])
        pull_domains = []
        for pull_request_names in pull_request_names:
            remote_short_name, name = pull_request_names.split('#')
            owner, repo_name = remote_short_name.split('/')
            pull_domains.append([('remote_id.owner', '=', owner), ('remote_id.repo_name', '=', repo_name), ('name', '=', name)])
        pull_domains = fields.Domain.OR(pull_domains)
        pull_requests = request.env['runbot.branch'].sudo().search([('is_pr', '=', True)] + pull_domains)
        bundles = pull_requests.bundle_id
        _logger.info('Received CI request for bundles: %s', bundles.mapped('name'))
        bundles._force_ci()
