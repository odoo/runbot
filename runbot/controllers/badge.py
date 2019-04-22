# -*- coding: utf-8 -*-
import hashlib

import werkzeug
from matplotlib.font_manager import FontProperties
from matplotlib.textpath import TextToPath

from odoo.http import request, route, Controller


class RunbotBadge(Controller):

    @route([
        '/runbot/badge/<int:repo_id>/<branch>.svg',
        '/runbot/badge/<any(default,flat):theme>/<int:repo_id>/<branch>.svg',
    ], type="http", auth="public", methods=['GET', 'HEAD'])
    def badge(self, repo_id, branch, theme='default'):

        domain = [('repo_id', '=', repo_id),
                  ('branch_id.branch_name', '=', branch),
                  ('branch_id.sticky', '=', True),
                  ('hidden', '=', False),
                  ('parent_id', '=', False),
                  ('global_state', 'in', ['testing', 'running', 'done']),
                  ('global_result', 'not in', ['skipped', 'manually_killed']),
                  ]

        last_update = '__last_update'
        builds = request.env['runbot.build'].sudo().search_read(
            domain, ['global_state', 'global_result', 'build_age', last_update],
            order='id desc', limit=1)

        if not builds:
            return request.not_found()

        build = builds[0]
        etag = request.httprequest.headers.get('If-None-Match')
        retag = hashlib.md5(build[last_update].encode()).hexdigest()

        if etag == retag:
            return werkzeug.wrappers.Response(status=304)

        if build['global_state'] in ('testing', 'waiting'):
            state = build['global_state']
            cache_factor = 1
        else:
            cache_factor = 2
            if build['global_result'] == 'ok':
                state = 'success'
            elif build['global_result'] == 'warn':
                state = 'warning'
            else:
                state = 'failed'

        # from https://github.com/badges/shields/blob/master/colorscheme.json
        color = {
            'testing': "#dfb317",
            'waiting': "#dfb317",
            'success': "#4c1",
            'failed': "#e05d44",
            'warning': "#fe7d37",
        }[state]

        def text_width(s):
            fp = FontProperties(family='DejaVu Sans', size=11)
            w, h, d = TextToPath().get_text_width_height_descent(s, fp, False)
            return int(w + 1)

        class Text(object):
            __slot__ = ['text', 'color', 'width']

            def __init__(self, text, color):
                self.text = text
                self.color = color
                self.width = text_width(text) + 10

        data = {
            'left': Text(branch, '#555'),
            'right': Text(state, color),
        }
        five_minutes = 5 * 60
        headers = [
            ('Content-Type', 'image/svg+xml'),
            ('Cache-Control', 'max-age=%d' % (five_minutes * cache_factor,)),
            ('ETag', retag),
        ]
        return request.render("runbot.badge_" + theme, data, headers=headers)
