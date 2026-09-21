import datetime
import ipaddress
import json
import logging
import secrets
import socket
from urllib.parse import urlencode, quote_plus, urlsplit, parse_qsl

import markupsafe
import requests

from odoo.http import Controller, Response, route, request
from odoo.tools import hmac

_logger = logging.getLogger(__name__)

class AppController(Controller):
    @route(
        '/runbot_merge/apps/create/<model("runbot_merge.github_connector.appwizard"):wizard>',
        auth='user',
        methods=["GET"],
    )
    def create(self, wizard, org=None) -> str | Response:
        if wizard.create_uid != request.env.user:
            return request.not_found()

        site_url = wizard.get_base_url()
        try:
            host = urlsplit(site_url).hostname
            ip = socket.gethostbyname(host)
        except Exception:
            # url parsing blows up, or dns resolution fails, fallback to private
            ip = '::1'
        if not ipaddress.ip_address(ip).is_global:
            return Response(markupsafe.Markup(
                "<p>Github requires a globally routable address, got {!r}</p>"
                "<p>Hint: use a tunnel / forwarder and update web.base.url</p>"
            ).format(site_url),
                status=403,
            )

        url = "https://github.com/{org}/settings/apps/new?{query}".format(
            org=f'/organizations/{quote_plus(org)}' if org else '',
            query=urlencode({'state': wizard.secret}),
        )
        manifest = markupsafe.escape(json.dumps({
            # User will have to use something else due to dup' names
            "name": "Mergebot",
            "url": site_url,
            # Necessary to install the app in multiple orgs, or have a user
            # authorize it after installing in an org account
            "public": True,
            # This could be non-specific and we embed the wizard id in the secret instead
            "redirect_url": f'{site_url}/runbot_merge/apps/create/{wizard.id}/created',
            # This can't be linked ot the wizard as that will have been deleted by then.
            "callback_urls": [f'{site_url}/runbot_merge/apps/authorized'],
            "hook_attributes": {"url": f'{site_url}/runbot_merge/hooks'},
            "default_permissions": {
                'contents': 'write',
                'deployments': 'write',
                'issues': 'write',
                'pull_requests': 'write',
                'statuses': 'read',
            },
            "default_events": ['pull_request', 'issue_comment', 'status', 'pull_request_review'],
        }, separators=(',', ':')))
        org = markupsafe.escape_silent(org)
        return f"""
<!DOCTYPE html>
<html lang="en">
<body>
    Create a github app. The app will be created in {org or "your account"}.
    <form action="" method="get">
        <input type="text" name="org" value="{org}">
        <input type="submit" value="change account">
    </form>
    <form action="{url}" method=POST>
        <input type=hidden name=manifest value={manifest}>
        <input type="submit" value="Create Application">
    </form>
</body>
</html>
            """

    @route(
        '/runbot_merge/apps/create/<model("runbot_merge.github_connector.appwizard"):wizard>/created',
        auth='user',
        methods=["GET"],
    )
    def created(self, wizard, code: str, state: str) -> str | Response:
        secret = wizard.secret
        project = wizard.project_id
        wizard.unlink()
        if state != secret:
            return Response("Incorrect state", status=400)

        r = requests.post(f"https://api.github.com/app-manifests/{code}/conversions")
        if not r.ok:
            _logger.info("Code conversion failed", r.json())
            return Response("Incorrect state", status=400)
        appinfo = r.json()

        project.github_id = request.env['runbot_merge.github_connector'].create({
            'method': 'app',
            'app_id': appinfo['id'],
            'name': appinfo['name'],
            'client_id': appinfo['client_id'],
            'client_secret': appinfo['client_secret'],
            'private_key': appinfo['pem'],
            'webhook_secret': appinfo['webhook_secret'],
        }).id

        return request.redirect(
            project.get_base_url() + '/web#' + urlencode({
                'model': 'runbot_merge.project',
                'id': project.id,
                'view_type': 'form',
            })
        )

    @route(
        '/runbot_merge/apps/authorized',
        auth='user',
        methods=['GET'],
    )
    def authorized(self, code: str, state: str, **_kwargs) -> str | Response:
        pid, gid, mac = state.split(':', 2)
        pid = int(pid)
        gid = int(gid)
        if not secrets.compare_digest(mac, hmac(request.env, 'app', (pid, gid))):
            return Response("Incorrect state", status=400)

        app = request.env['runbot_merge.github_connector'].browse(gid)
        r = requests.post(
            'https://github.com/login/oauth/access_token',
            params={'client_id': app.client_id, 'client_secret': app.client_secret, 'code': code}
        )
        if not r.ok:
            _logger.info("Code conversion failed", r.json())
            return Response("Incorrect state", status=400)

        res = dict(parse_qsl(r.text))
        app.auth_token = res['access_token']
        if refresh := res.get('refresh_token'):
            app.refresh_token = refresh
            app.auth_expires_at = datetime.datetime.now() + datetime.timedelta(
                seconds=int(res['expires_in']) - 660
            )

        project = request.env['runbot_merge.project'].browse(pid)
        return request.redirect(
            project.get_base_url() + '/web#' + urlencode({
                'model': 'runbot_merge.project',
                'id': project.id,
                'view_type': 'form',
            })
        )