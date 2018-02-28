# -*- coding: utf-8 -*-

import datetime

from odoo import http, tools
from odoo.http import request


class RunbotHook(http.Controller):

    @http.route(['/runbot/hook/<int:repo_id>'], type='http', auth="public", website=True)
    def hook(self, repo_id=None, **post):
        # TODO if repo_id == None parse the json['repository']['ssh_url'] and find the right repo
        repo = request.env['runbot.repo'].sudo().browse([repo_id])
        repo.hook_time = datetime.datetime.now().strftime(tools.DEFAULT_SERVER_DATETIME_FORMAT)
        return ""
