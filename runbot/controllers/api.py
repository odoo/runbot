import json
import logging
import time

from functools import wraps

from odoo import http
from odoo.http import request
from odoo.exceptions import AccessError, AccessDenied, UserError
from odoo.tools import consteq

_logger = logging.getLogger(__name__)

def to_json(fn):
    @wraps(fn)
    def decorator(*args, **kwargs):
        headers = [('Content-Type', 'application/json'),
                ('Cache-Control', 'no-store')]
        try:
            return request.make_response(json.dumps(fn(*args, **kwargs), indent=4, default=str), headers)
        except AccessError:
            response = request.make_response(json.dumps('unauthorized'), headers)
            response.status = 401
            return response
        except AccessDenied as e:
            response = request.make_response(json.dumps(e.args[0]), headers)
            response.status = 403
            return response
        except UserError as e:
            response = request.make_response(json.dumps(e.args[0]), headers)
            response.status = 400
            return response
    return decorator

class RunbotController(http.Controller):

    @http.route('/runbot/api/web_search_read', type='http', auth='public', csrf=False)
    @to_json
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
        try:
            assert user.active  # handle unexisting user, accessing a field raises a user error
        except UserError:
            time.sleep(1)
            raise AccessDenied(message={'error': 'Unauthorized'})

        if not user or not user.runbot_api_token or not token or len(token) < 32 or not consteq(user.runbot_api_token, token):
            time.sleep(1)
            raise AccessDenied(message={'error': 'Invalid user or token'})
        request.env.cache.clear()
        limit = max(min(2000, limit), 1)
        if not model.startswith('runbot.'):
            raise UserError({'error': 'Invalid model'})
        if id:
            ids = [id]
        if ids:
            domain = [('id', '=', ids)]
        else:
            domain = json.loads(domain)
        if not domain:
            raise UserError({'error': 'Invalid domain'})
        specification = json.loads(specification)

        try:
            user_env = request.env(user=user)
            result = user_env[model].web_search_read(domain, specification, limit=limit, offset=offset, order=order)
            return result
        except Exception:
            _logger.exception('Something went wrong reading %s %s %s', model, specification, domain)
            raise UserError({'error': 'Something went wrong'})
