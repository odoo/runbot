from __future__ import annotations

from werkzeug.exceptions import BadRequest, Forbidden

from typing import Dict, Union, List, Self, TypedDict

from odoo import models, api, fields, tools
from odoo.osv import expression


class SubSpecification(TypedDict):
    context: Dict
    fields: Dict[str, 'SubSpecification']
Specification = Dict[str, Union[Dict, 'SubSpecification']]

SUPPORTED_FIELD_TYPES = { # Perhaps this should be a list of class instead
    'boolean', 'integer', 'float', 'char', 'text', 'html',
    'date', 'datetime', 'selection', 'jsonb',
    'many2one', 'one2many', 'many2many',
}
RELATIONAL_FIELD_TYPES = {'many2one', 'one2many', 'many2many'}
SPEC_MAX_DEPTH = 10
SPEC_METADATA_FIELD = {
    '__type', '__help',
}
DEFAULT_LIMIT = 20
DEFAULT_MAX_LIMIT = 60

def _cleaned_spec(spec: Specification | SubSpecification) -> Specification | SubSpecification:
    """ Returns the specification without metadata fields. """
    if not isinstance(spec, dict):
        return spec
    return {
        k: v for k, v in spec.items()
        if k not in SPEC_METADATA_FIELD
    }

class PublicModelMixin(models.AbstractModel):
    _name = 'runbot.public.model.mixin'
    _description = 'Mixin for publicly accessible data'

    @api.model
    def _valid_field_parameter(self, field: fields.Field, name: str):
        if field.type in SUPPORTED_FIELD_TYPES:
            return name in (
                # boolean, whether the field is readable through the public api,
                # public fields on record on which the user does not have access are not exposed.
                'public',
            ) or super()._valid_field_parameter(field, name)
        return super()._valid_field_parameter(field, name)

    @api.model
    def _get_public_fields(self) -> List[fields.Field]:
        """ Returns a list of publicly readable fields. """
        return [
            field for field in self._fields.values()
            if getattr(field, 'public', None) or field.name == 'id'
        ]

    ########## REQUESTS ##########

    @api.model
    def _api_request_allow_direct_access(self) -> bool:
        """ Returns whether this model is accessible directly through the api. """
        return True

    @api.model
    def _api_request_allowed_keys(self) -> set[str]:
        """ Returns a list of allowed keys for request_data. """
        return self._api_request_required_keys() | {
            'context',
            'limit', 'offset',
        }

    @api.model
    def _api_request_default_limit(self) -> int:
        return DEFAULT_LIMIT

    @api.model
    def _api_request_max_limit(self) -> int:
        return DEFAULT_MAX_LIMIT

    @api.model
    def _api_request_required_keys(self) -> set[str]:
        """ Returns a list of required keys for request_data. """
        required_keys = {'specification', 'domain'}
        if self._api_request_requires_project():
            required_keys.add('project_id')
        return required_keys

    @api.model
    def _api_request_requires_project(self) -> bool: #TODO: rename me
        """ Public models are by default based on a project_id (filtered on project_id). """
        return self._api_request_allow_direct_access()

    @api.model
    def _api_project_id_field_path(self) -> str:
        """ Returns the path from the current object to project_id. """
        raise NotImplementedError('_api_project_id_field_path not implemented')

    @api.model
    def _api_request_validate_domain(self, domain: list[str | tuple | list]):
        """
        Validates a domain against the public spec.

        This only validates that all the fields in the domain are queryable fields,
            the actual validity of the domain will be checked by the orm when
            searching for records.

        Returns:
            domain: a transformed domain if necessary

        Raises:
            AssertionError: unknown domain leaf
            Forbidden: invalid field used
        """

        try:
            self._where_calc(domain)
        except ValueError as e:
            raise BadRequest('Invalid domain') from e

        spec: Specification = self._api_public_specification()
        # recompiles the spec into a list of fields that can be present in the domain
        valid_fields: str[str] = set()
        def _visit_spec(spec, prefix: str | None = None):
            spec = _cleaned_spec(spec)
            if not spec:
                return
            for field, sub_spec in spec.items():
                this_field = f'{prefix}.{field}' if prefix else field
                valid_fields.add(this_field)
                if sub_spec and sub_spec.get('fields'):
                    _visit_spec(sub_spec['fields'], prefix=this_field)
        _visit_spec(spec)

        for leaf in domain:
            if not isinstance(leaf, (tuple, list)):
                continue
            assert len(leaf) == 3 # Can this happen in a valid domain?
            if leaf[0] not in valid_fields and not self.env.user.has_group('runbot.group_runbot_admin'):
                raise Forbidden('Trying to filter from private field')

        if self._api_request_requires_project():
            assert 'project_id' in self.env.context
            domain = expression.AND([
                [(self._api_project_id_field_path(), '=', self.env.context['project_id'])],
                domain
            ])

        return domain

    @api.model
    def _api_request_read_get_offset_limit(self, request_data: dict) -> tuple[int, int]:
        if 'limit' in request_data:
            if not isinstance(request_data['limit'], int):
                raise BadRequest('Invalid page size (should be int)')
            limit = request_data['limit']
            if limit > self._api_request_max_limit():
                raise BadRequest('Page size exceeds max size')
        else:
            limit = self._api_request_default_limit()
        offset = 0
        if 'offset' in request_data:
            if not isinstance(request_data['offset'], int):
                raise BadRequest('Invalid page (should be int)')
            offset = request_data['offset']
        return limit, offset

    @api.model
    def _api_request_read_get_records(self, request_data: dict) -> Self:
        limit, offset = self._api_request_read_get_offset_limit(request_data)
        return self.search(request_data['domain'], limit=limit, offset=offset)

    @api.model
    def _api_request_read(self, request_data: dict) -> list[dict]:
        """
        Processes a frontend request and returns the data to be returned by the controller.

        This method is allowed to raise Http specific exceptions.
        """
        specification, domain = request_data['specification'], request_data['domain']

        try:
            if not self._api_verify_specification(specification) and\
                not self.env.user.has_group('runbot.group_runbot_admin'):
                raise Forbidden('Invalid specification or trying to access private data.')
        except (ValueError, AssertionError) as e:
            raise BadRequest('Invalid specification') from e

        request_data['domain'] = self._api_request_validate_domain(domain)
        records = self._api_request_read_get_records(request_data)

        return records._api_read(request_data['specification'])

    ########## SPEC ##########

    @api.model
    def _api_get_relation_field_key(self, field: fields.Field):
        """ Returns a relation cache key for a field, a string defining the identity of the relationship. """
        if isinstance(field, fields.Many2one):
            return f'{self._name}__{field.name}'
        elif isinstance(field, fields.Many2many):
            if not field.store:
                return f'{self._name}__{field.name}'
            return field.relation
        elif isinstance(field, fields.One2many):
            if not field.store: # is this valid?
                return f'{self._name}__{field.name}'
            CoModel: PublicModelMixin = self.env[field.comodel_name]
            inverse_field = CoModel._fields[field.inverse_name]
            return CoModel._api_get_relation_field_key(inverse_field)
        raise NotImplementedError('Unsupported field')

    @tools.ormcache()
    @api.model
    def _api_public_specification(self) -> Specification:
        """
        Returns the public specification for the model.

        The specification will go through all the fields marked as public.
        For relational fields, the result will be nested (up to a depth of :code:`SPEC_MAX_DEPTH`).

        The specification will contain metadata about each fields.
        The specification returned by this method can be used directly with :code:`_api_read`.

        Returns:
            specification: The specification as a dictionary.
        """
        # We want to prevent infinite loops so we need to track which relations
        # have already been explored, this concerns many2one, many2many
        def _visit_model(model: PublicModelMixin, visited_relations: set[str], depth = 0) -> Specification | SubSpecification:
            spec: Specification | SubSpecification = {}
            for field in model._get_public_fields():
                field_metadata = {
                    '__type': field.type,
                }
                if field.help:
                    field_metadata['__help'] = field.help
                if field.relational and \
                    issubclass(self.pool[field.comodel_name], PublicModelMixin):
                    field_key = model._api_get_relation_field_key(field)
                    if field_key in visited_relations or depth == SPEC_MAX_DEPTH:
                        continue
                    visited_relations.add(field_key)
                    CoModel: PublicModelMixin = model.env[field.comodel_name]
                    field_metadata.update(
                        fields=_visit_model(CoModel, {*visited_relations}, depth + 1)
                    )
                spec[field.name] = field_metadata
            return spec

        return _visit_model(self, set())
    
    @api.model
    def _api_verify_specification(self, specification: Specification) -> bool:
        """
        Verifies a given specification against the public specification.

        This step also provides some validation of the specification, enough that
            the spec can be safely used with `_api_read` if the method does not
            raise an exception.

        Args:
            specification: The requested specification.

        Returns:
            If the spec matches the public spec this method returns True
                otherwise False.

        Raises:
            ValueError: If a sub spec is given for a non relational field.
            ValueError: If a sub spec is given for a relational field that does
                not allow public data (id only).
        """
        public_specification: Specification = self._api_public_specification()

        def _visit_spec(
            model_spec: Specification,
            request_spec: Specification,
        ) -> bool:
            request_spec = _cleaned_spec(request_spec)
            for field, sub_spec in request_spec.items():
                sub_spec = _cleaned_spec(sub_spec)
                if field not in model_spec:
                    return False
                if not isinstance(sub_spec, dict):
                    raise ValueError(
                        'Invalid sub spec, should be a dict.'
                    )
                # For now we actually only have keys for relational fields.
                sub_spec_allowed_keys = set()
                if model_spec[field].get('__type') in RELATIONAL_FIELD_TYPES\
                    and 'fields' in model_spec[field]:
                    sub_spec_allowed_keys.add('fields')
                    sub_spec_allowed_keys.add('context')
                if set(sub_spec.keys()) - sub_spec_allowed_keys:
                    raise ValueError(
                        'Invalid sub spec, contains unknown keys.'
                    )
                if not sub_spec or 'fields' not in sub_spec:
                    continue
                if 'fields' not in model_spec[field]:
                    raise ValueError(
                        f'Sub spec not available for field {field}'
                    )
                if not _visit_spec(model_spec[field]['fields'], sub_spec['fields']):
                    return False
            return True

        return _visit_spec(public_specification, specification)
    
    def _api_read(self, specification: Specification) -> list[dict]:
        """ Forwards the specification to `web_read`. """
        return self.web_read(specification)
