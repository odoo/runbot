import json

from werkzeug.exceptions import BadRequest, Forbidden

from odoo.osv import expression
from odoo.tests.common import HttpCase, TransactionCase, tagged, new_test_user

from odoo.addons.runbot.models.public_model_mixin import PublicModelMixin


@tagged('-at_install', 'post_install')
class TestPublicApi(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.project = cls.env['runbot.project'].create({'name': 'Tests', 'process_delay': 0})

    def get_public_models(self):
        for Model in self.registry.values():
            if not issubclass(Model, PublicModelMixin) or Model._name == 'runbot.public.model.mixin':
                continue
            yield self.env[Model._name]


    def test_requires_project_defines_project_id_path(self):
        for Model in self.get_public_models():
            if not Model._api_request_requires_project():
                continue
            # Try calling _api_project_id_field_path, none should fail
            with self.subTest(model=Model._name):
                try:
                    Model._api_project_id_field_path()
                except NotImplementedError:
                    self.fail('_api_project_id_field_path not implemented')

    def test_direct_access_disabled(self):
        DisabledModel = self.env['runbot.commit.link']
        self.assertFalse(DisabledModel._api_request_allow_direct_access())

        resp = self.url_open('/runbot/api/models')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn(DisabledModel._name, data)

        resp = self.url_open(f'/runbot/api/{DisabledModel._name}/spec')
        self.assertEqual(resp.status_code, 403)

        resp = self.url_open(f'/runbot/api/{DisabledModel._name}/read', data="{}", headers={'Content-Type': 'application/json'}) # model checking happens before data checking
        self.assertEqual(resp.status_code, 403)

    def test_api_public_basics(self):
        # This serves as a basic read test through the api
        Model = self.env['runbot.bundle']
        self.assertTrue(Model._api_request_allow_direct_access())

        resp = self.url_open('/runbot/api/models')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn(Model._name, data)

        resp = self.url_open(f'/runbot/api/{Model._name}/spec')
        self.assertEqual(resp.status_code, 200)
        
        request_data = json.dumps({
            'domain': [],
            'specification': resp.json()['specification'],
            'project_id': self.project.id,
        })
        resp = self.url_open(f'/runbot/api/{Model._name}/read', data=request_data, headers={'Content-Type': 'application/json'})
        self.assertEqual(resp.status_code, 200)

    def test_api_read_from_spec_public_models(self):
        # This is not ideal as we don't have any data but it is better than nothing
        for Model in self.get_public_models():
            if not Model._api_request_allow_direct_access():
                continue
            with self.subTest(model=Model._name):
                resp = self.url_open(f'/runbot/api/{Model._name}/spec')
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                if set(data['required_keys']) > self.env['runbot.public.model.mixin']._api_request_required_keys():
                    self.skipTest('Skipping, request requires unknown keys, create a specific test')
                request_data = {
                    'domain': [],
                    'specification': data['specification'],
                }
                if Model._api_request_requires_project():
                    request_data['project_id'] = self.project.id
                request_data = json.dumps(request_data)
                resp = self.url_open(f'/runbot/api/{Model._name}/read', data=request_data, headers={'Content-Type': 'application/json'})
                self.assertEqual(resp.status_code, 200)

    def test_api_read_homepage(self):
        # Arbitrary test testing the initial schema required for the homepage
        # We only check that the response is successful
        request_data = json.dumps({
            'domain': [['last_batch', '!=', False]],
            'project_id': self.project.id,
            # 'category_id': False Ignored for the sake of the test
            'specification': {
                "name": {},
                "branch_ids": {
                    "fields": {
                        "dname": {},
                        "branch_url": {}
                    }
                },
                "last_batchs": {
                    "fields": {
                        "age": {},
                        "last_update": {},
                        "slot_ids": {
                            "fields": {
                                "link_type": {},
                                "trigger_id": {
                                    "fields": {
                                        "name": {}
                                    }
                                },
                                "build_id": {
                                    "fields": {
                                        "local_state": {},
                                        "local_result": {},
                                        "global_state": {},
                                        "global_result": {},
                                        "requested_action": {},
                                        "log_list": {},
                                        "version_id": {},
                                        "config_id": {},
                                        "trigger_id": {},
                                        "create_batch_id": {},
                                        "host_id": {
                                            "fields": {
                                                "name": {}
                                            }
                                        },
                                        "database_ids": {
                                            "fields": {
                                                "name": {}
                                            }
                                        }
                                    }
                                }
                            }
                        },
                        "commit_link_ids": {
                            "fields": {
                                "match_type": {},
                                "commit_id": {
                                    "fields": {
                                        "dname": {},
                                        "subject": {}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        })
        resp = self.url_open('/runbot/api/runbot.bundle/read', data=request_data, headers={'Content-Type': 'application/json'})
        resp.raise_for_status()

@tagged('-at_install', 'post_install')
class TestPublicModelApi(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env['runbot.project'].create({'name': 'Tests', 'process_delay': 0})
        self.basic_user = new_test_user(self.env, 'runbot')
        self.uid = self.basic_user
        # Context key used in some tests.
        self.BundleModel = self.env['runbot.bundle']\
            .with_context(project_id=self.project.id)\
            .with_user(self.basic_user)

    def test_invalid_domain(self):
        # Unknown field
        with self.assertRaises(BadRequest):
            self.BundleModel._api_request_validate_domain([['booger', '=', 1]])

        # Private field
        self.assertFalse(
            getattr(self.BundleModel._fields['modules'], 'public', False),
            'modules field is not private anymore, change to another private field',
        )
        with self.assertRaises(Forbidden):
            self.BundleModel._api_request_validate_domain(
                [('modules', '=', 1)]
            )

    def test_valid_domain_add_project_id(self):
        self.assertTrue(self.BundleModel._api_request_requires_project())

        self.assertEqual(
            self.BundleModel._api_request_validate_domain([]),
            [('project_id', '=', self.project.id)]
        )

    def test_valid_domain(self):
        domain = [
            ('name', '=', 'master'), # Basic field
            ('project_id.name', '=', 'R&D'), # 1-level related field
            ('project_id.trigger_ids.name', '=', 'Enterprise run'), # 2-level related field
        ]
        self.assertListEqual(
            self.BundleModel._api_request_validate_domain(domain),
            expression.AND([
                [('project_id', '=', self.project.id)],
                domain,
            ])
        )

    def test_process_read_limits(self):
        request_data = {
            'domain': [],
            'specification': {},
        }
        # Test with non int limit
        with self.assertRaises(BadRequest):
            request_data['limit'] = 'test'
            self.BundleModel._api_request_read(request_data)
        # Test with limit above max
        with self.assertRaises(BadRequest):
            request_data['limit'] = self.BundleModel._api_request_max_limit() + 10
            self.BundleModel._api_request_read(request_data)
        request_data.pop('limit')
        # Test with invalid offset
        with self.assertRaises(BadRequest):
            request_data['offset'] = 'test'
            self.BundleModel._api_request_read(request_data)

    def test_verify_spec_invalid(self):
        check = self.BundleModel._api_verify_specification
        # Test with unknown field
        self.assertFalse(
            check({
                'invalid_field': {}
            })
        )
        self.assertFalse(
            check({
                'project_id': {
                    'fields': {
                        'invalid_field': {}
                    }
                }
            })
        )
        # Test with sub_spec not dict
        with self.assertRaises(ValueError):
            check({
                'name': ['i', 'don\'t', 'know']
            })
        # Test with unknown key in dict
        with self.assertRaises(ValueError):
            check({
                'name': {'fields': {}} # Non relational fields do not allow 'fields'
            })
