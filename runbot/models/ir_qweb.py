from ..common import s2human, s2human_long
from odoo import models
from odoo.http import request
from odoo.addons.http_routing.models.ir_http import slug
from odoo.addons.website.controllers.main import QueryURL


class IrQweb(models.AbstractModel):
    _inherit = ["ir.qweb"]

    def _prepare_frontend_environment(self, values):
        response = super()._prepare_frontend_environment(values)

        has_pr = values.get('has_pr', None)
        search = values.get('search', None)
        refresh = values.get('refresh', None)
        project = values.get('project', None)

        values['more'] = values.get('more', request.httprequest.cookies.get('more', False) == '1')
        values['theme'] = values.get('theme', request.httprequest.cookies.get('theme', 'legacy'))
        values['filter_mode'] = values.get('filter_mode', request.httprequest.cookies.get('filter_mode', 'all'))

        values['s2human'] = s2human
        values['s2human_long'] = s2human_long
        if 'projects' not in values:
            values['projects'] = request.env['runbot.project'].search([('hidden', '=', False)])

        values['qu'] = QueryURL('/runbot/%s' % (slug(project) if project else ''), search=search, refresh=refresh, has_pr=has_pr)

        if 'title' not in values and project:
            values['title'] = 'Runbot %s' % project.name or ''

        values['nb_build_errors'] = request.env['runbot.build.error'].search_count([('random', '=', True), ('parent_id', '=', False)])
        values['nb_assigned_errors'] = request.env['runbot.build.error'].search_count([('responsible', '=', request.env.user.id)])
        values['nb_team_errors'] = request.env['runbot.build.error'].search_count([('responsible', '=', False), ('team_id', 'in', request.env.user.runbot_team_ids.ids)])

        values['current_path'] = request.httprequest.full_path
        values['default_category'] = request.env['ir.model.data']._xmlid_to_res_id('runbot.default_category')

        return response
