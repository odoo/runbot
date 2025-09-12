from ..common import s2human, s2human_long, precise_s2human
from odoo import models
from odoo.http import request
from odoo.addons.website.controllers.main import QueryURL

class IrQweb(models.AbstractModel):
    _inherit = "ir.qweb"

    def _prepare_frontend_environment(self, values):
        kwargs = request.params
        projects = values.get('projects', self.env['runbot.project'].search([('hidden', '=', False)]))
        project = kwargs.get('project') or (projects and projects[0])
        values['more'] = request.httprequest.cookies.get('more', False) == '1'
        values['filter_mode'] = request.httprequest.cookies.get('filter_mode', 'default')

        values['refresh'] = kwargs.get('refresh', False)
        values['search'] = kwargs.get('search', '')  # kwargs
        values['has_pr'] = kwargs.get('has_pr')  # kwargs
        values['project'] = project

        values['qu'] = QueryURL('/runbot/%s' % (request.env['ir.http']._slug(values['project']) if values['project'] else ''), search=values['search'], refresh=values['refresh'], has_pr=values['has_pr'])
        values['theme'] = kwargs.get('theme', request.httprequest.cookies.get('theme', 'legacy'))

        values['s2human'] = s2human
        values['s2human_long'] = s2human_long
        values['precise_s2human'] = precise_s2human

        values['default_category'] = request.env['ir.model.data']._xmlid_to_res_id('runbot.default_category')

        values['current_path'] = request.httprequest.full_path

        # errors counters
        if self.env.user.is_public():
            nb_build_errors = nb_assigned_errors = nb_team_errors = 0
        else:
            nb_build_errors = request.env['runbot.build.error'].search_count([])
            nb_assigned_errors = request.env['runbot.build.error'].search_count([('responsible', '=', request.env.user.id)])
            nb_team_errors = request.env['runbot.build.error'].search_count([('responsible', '=', False), ('team_id', 'in', request.env.user.runbot_team_ids.ids)])
        values['nb_build_errors'] = nb_build_errors
        values['nb_assigned_errors'] = nb_assigned_errors
        values['nb_team_errors'] = nb_team_errors

        if 'title' not in values:
            values['title'] = 'Runbot %s' % project.name or ''

        if 'page_info_state' not in values:
            values['page_info_state'] = 'ok'

        return super()._prepare_frontend_environment(values)
