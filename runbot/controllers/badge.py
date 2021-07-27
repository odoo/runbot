# -*- coding: utf-8 -*-
import hashlib

import werkzeug
from matplotlib.font_manager import FontProperties
from matplotlib.textpath import TextToPath

from odoo.http import request, route, Controller


class RunbotBadge(Controller):

    @route([
        '/runbot/badge/<int:repo_id>/<name>.svg',
        '/runbot/badge/trigger/<int:trigger_id>/<name>.svg',
        '/runbot/badge/<any(default,flat):theme>/<int:repo_id>/<name>.svg',
        '/runbot/badge/trigger/<any(default,flat):theme>/<int:trigger_id>/<name>.svg',
    ], type="http", auth="public", methods=['GET', 'HEAD'], sitemap=False)
    def badge(self, name, repo_id=False, trigger_id=False, theme='default'):
        # Sudo is used here to allow the badge to be returned for projects
        # which have restricted permissions.
        Trigger = request.env['runbot.trigger'].sudo()
        Repo = request.env['runbot.repo'].sudo()
        Batch = request.env['runbot.batch'].sudo()
        Bundle = request.env['runbot.bundle'].sudo()
        if trigger_id:
            triggers = Trigger.browse(trigger_id)
            project = triggers.project_id
        else:
            triggers = Trigger.search([('repo_ids', 'in', repo_id)])
            project = Repo.browse(repo_id).project_id
            # -> hack to use repo. Would be better to change logic and use a trigger_id in params
        bundle = Bundle.search([('name', '=', name),
            ('project_id', '=', project.id)])
        if not bundle or not triggers:
            return request.not_found()
        batch = Batch.search([
            ('bundle_id', '=', bundle.id),
            ('state', '=', 'done'),
            ('category_id', '=', request.env.ref('runbot.default_category').id)
        ], order='id desc', limit=1)

        builds = batch.slot_ids.filtered(lambda s: s.trigger_id in triggers).mapped('build_id')
        if not builds:
            state = 'testing'
        else:
            result = builds.result_multi()
            if result == 'ok':
                state = 'success'
            elif result == 'warn':
                state = 'warning'
            else:
                state = 'failed'

        etag = request.httprequest.headers.get('If-None-Match')
        retag = hashlib.md5(state.encode()).hexdigest()
        if etag == retag:
            return werkzeug.wrappers.Response(status=304)

        # from https://github.com/badges/shields/blob/master/colorscheme.json
        color = {
            'testing': "#dfb317",
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
            'left': Text(name, '#555'),
            'right': Text(state, color),
        }
        headers = [
            ('Content-Type', 'image/svg+xml'),
            ('Cache-Control', 'max-age=%d' % (10*60,)),
            ('ETag', retag),
        ]
        return request.render("runbot.badge_" + theme, data, headers=headers)
