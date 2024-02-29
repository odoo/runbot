import json
import logging
import time

from odoo import http
from odoo.http import request
from odoo.tools import consteq

_logger = logging.getLogger(__name__)


class RunbotController(http.Controller):

    @http.route('/runbot/api/web_search_read', type='http', auth='public', csrf=False)
    def api_web_search_read(self, uid=None, token=None, model=None, specification=None, id=None, ids=None, domain=None, limit=200, offset=0, order=None):
        """
            model: the model to read
            fields: dictionnary

            Example of usage:

            requests.post(
                'http://127.0.0.1:8069/runbot/api/read',
                data={
                    'uid': <uid>
                    'token': '<token>',
                    'model': 'runbot.bundle',
                    'domain':json.dumps([('sticky', '=', True), ('project_id', '=', 1)]),
                    'specification': json.dumps({
                        "id": {},
                        'name': {},
                        'last_done_batch': {
                            'fields': {
                                'commit_ids': {
                                    'fields': {
                                        'name': {}
                                    }
                                }
                            }
                        }
                    })
                }
            ).json()
        """
        user = request.env['res.users'].sudo().browse(int(uid))
        if not user or not token or len(token) < 32 or not consteq(user.runbot_api_token, token):
            time.sleep(1)
            return json.dumps({'error': 'Invalid user or token'})
        request.env.cache.clear()
        limit = max(min(2000, limit), 1)
        if not model.startswith('runbot.'):
            return json.dumps({'error': 'Invalid model'})
        if id:
            ids = [id]
        if ids:
            domain = [('id', '=', ids)]
        else:
            domain = json.loads(domain)
        if not domain:
            return json.dumps({'error': 'Invalid domain'})
        specification = json.loads(specification)

        try:
            user_env = request.env(user=user)
            result = user_env[model].web_search_read(domain, specification, limit=limit, offset=offset, order=order)
            return json.dumps(result)
        except Exception:
            _logger.exception('Something went wrong reading %s %s %s', model, specification, domain)
            return json.dumps({'error': 'Something went wrong'})
