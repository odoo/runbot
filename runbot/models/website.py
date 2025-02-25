from odoo import models, fields, api
from odoo.http import request
from odoo.addons.web.controllers.utils import get_action_triples
import re


class Website(models.AbstractModel):
    _inherit = "website.seo.metadata"

    def get_website_meta(self):
        # this is kind of hacky but should improve user experience when sharing runbot links
        # right now, a backend link will lead to the login page creating a link preview of the login page.
        # this override will hopefully remove the website image and try to improve the meta based on the record
        # in the backend, if possible to extract
        res = super().get_website_meta()
        del res['opengraph_meta']['og:image']
        del res['twitter_meta']
        if request and request.params.get('redirect') and not request.params.get('login_success'):
            redirect = request.params['redirect']
            if redirect.startswith('/odoo/'):
                try:
                    actions = list(get_action_triples(self.env, redirect.split('?')[0].removeprefix('/odoo/')))
                except ValueError:
                    actions = None
                if actions:
                    _active_id, action, record_id = actions[-1]
                    model = action.res_model
                    record = self.env[model]
                    if record_id and model.startswith('runbot.'):
                        record = self.env[model].browse(record_id).exists()
                        if record.sudo(False)._check_access('read'):
                            record = self.env[model]
                    title = f'{record._description}'
                    if record:
                        title = f'{record._description} | {record.display_name}'
                        if 'description' in record._fields:
                            res['opengraph_meta']['og:description'] = record.description
                    res['opengraph_meta']['og:title'] = title
                    res['opengraph_meta']['og:url'] = request.httprequest.url_root.strip('/') + redirect
            return res