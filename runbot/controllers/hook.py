# -*- coding: utf-8 -*-

import time
import json
import logging
import hashlib
import hmac

from werkzeug.exceptions import BadRequest

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


def verify_signature(payload_body, remote, signature_header):
    """Verify that the payload was sent from GitHub by validating SHA256.

    Raise and return 403 if not authorized.

    Args:
        payload_body: original request body to verify (request.body())
        remote: runbot.remote
        signature_header: header received from GitHub (x-hub-signature-256)
    """
    if not remote.webhook_secret:
        return
    if not signature_header:
        _logger.info('Received payload without signature header')
        raise BadRequest(description="x-hub-signature-256 header is missing!")
    hash_object = hmac.new(remote.webhook_secret.encode('utf-8'), msg=payload_body, digestmod=hashlib.sha256)
    expected_signature = "sha256=" + hash_object.hexdigest()
    if not hmac.compare_digest(expected_signature, signature_header):
        _logger.info('Received payload with invalid signature for remote %s', remote.name)
        raise BadRequest(description="Request signatures didn't match!")


class Hook(http.Controller):

    @http.route(['/runbot/hook', '/runbot/hook/<int:remote_id>'], type='http', auth="public", website=True, csrf=False, sitemap=False)
    def hook(self, remote_id=None, **_post):
        event = request.httprequest.headers.get("X-Github-Event")
        payload_str = request.params.get('payload', '{}')
        payload = json.loads(payload_str)
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
        verify_signature(
            payload_str.encode('utf-8'), remote.webhook_secret,
            request.httprequest.headers.get('X-Hub-Signature-256')
        )

        # force update of dependencies too in case a hook is lost
        if not payload or event == 'push':
            remote.repo_id._set_hook_time(time.time())
        else:
            request.env['runbot.repo.hook.payload'].sudo().create({
                'remote_id': remote.id,
                'payload': payload,
                'event': event,
            })
        return ""
