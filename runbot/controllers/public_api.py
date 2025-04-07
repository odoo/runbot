import json

from werkzeug.exceptions import BadRequest, Forbidden

from odoo.exceptions import AccessError
from odoo.http import Controller, request, route
from odoo.tools import mute_logger

from odoo.addons.runbot.models.public_model_mixin import PublicModelMixin


class PublicApi(Controller):

    @mute_logger('odoo.addons.base.models.ir_model') # We don't care about logging acl errors
    def _get_model(self, model: str) -> PublicModelMixin:
        """
        Returns the model from a model string.

        Raises the appropriate exception if:
            - The model does not exist
            - The model is not a public model
            - The current user can not read the model
        """
        pool = request.env.registry
        try:
            Model = pool[model]
        except KeyError:
            raise BadRequest('Unknown model')
        if not issubclass(Model, pool['runbot.public.model.mixin']):
            raise BadRequest('Unknown model')
        Model = request.env[model]
        Model.check_access('read')
        if not Model._api_request_allow_direct_access():
            raise Forbidden('This model does not allow direct access')
        return Model

    @route('/runbot/api/models', auth='public', methods=['GET'], readonly=True)
    def models(self):
        models = []
        for model in request.env.keys():
            try:
                models.append(self._get_model(model))
            except (BadRequest, AccessError, Forbidden):
                pass
        return request.make_json_response(
            [Model._name for Model in models]
        )

    @route('/runbot/api/<model>/read', auth='public', methods=['POST'], readonly=True, csrf=False)
    def read(self, *, model: str):
        Model = self._get_model(model)
        required_keys = Model._api_request_required_keys()
        allowed_keys = Model._api_request_allowed_keys()
        try:
            data = request.get_json_data()
        except json.JSONDecodeError:
            raise BadRequest('Invalid payload, missing or malformed json')
        if not isinstance(data, dict):
            raise BadRequest('Invalid payload, should be a dict.')
        if (missing_keys := required_keys - set(data.keys())):
            raise BadRequest(f'Invalid payload, missing keys: {", ".join(missing_keys)}')
        if (unknown_keys := set(data.keys()) - allowed_keys):
            raise BadRequest(f'Invalid payload, unknown keys: {", ".join(unknown_keys)}')
        if 'context' in data:
            Model = Model.with_context(**data['context'])
        if Model._api_request_requires_project():
            if not isinstance(data['project_id'], int):
                raise BadRequest('Invalid project_id, should be an int')
            # This is an additional layer of protection for project_id
            project = request.env['runbot.project'].browse(data['project_id']).exists()
            if not project:
                raise BadRequest('Unknown project_id')
            project.check_access('read')
            Model = Model.with_context(project_id=project.id)
        return request.make_json_response(Model._api_request_read(data))

    @route('/runbot/api/<model>/spec', auth='public', methods=['GET'], readonly=True)
    def spec(self, *, model: str):
        Model = self._get_model(model)
        required_keys = Model._api_request_required_keys()
        allowed_keys = Model._api_request_allowed_keys()
        return request.make_json_response({
            'requires_project': Model._api_request_requires_project(),
            'default_page_size': Model._api_request_default_limit(),
            'max_page_size': Model._api_request_max_limit(),
            'required_keys': list(Model._api_request_required_keys()),
            'allowed_keys': list(allowed_keys - required_keys),
            'specification': self._get_model(model)._api_public_specification(),
        })
