from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import HttpCase, tagged

from ..common import get_details
from .common import RunbotCase

JSON_HEADERS = {'Accept': 'application/json'}


@tagged('-at_install', 'post_install')
class TestJsonVariant(RunbotCase, HttpCase):

    def setUp(self):
        super().setUp()
        self.build = self.Build.create({
            'params_id': self.base_params.id,
            'port': '1234',
            'local_result': 'ok',
        })
        self.env['runbot.batch.slot'].create({
            'batch_id': self.dev_batch.id,
            'trigger_id': self.trigger_server.id,
            'params_id': self.base_params.id,
            'build_id': self.build.id,
        })

    def test_html_by_default(self):
        response = self.url_open(f'/runbot/build/{self.build.id}')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['Content-Type'].startswith('text/html'))
        self.assertEqual(response.headers['Vary'], 'Accept')

    def test_build_json(self):
        response = self.url_open(f'/runbot/build/{self.build.id}', headers=JSON_HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Vary'], 'Accept')
        details = response.json()
        self.assertEqual(details['id'], self.build.id)
        self.assertEqual(details['params_id'], {'id': self.base_params.id, 'display_name': self.base_params.display_name})
        self.assertNotIn('log_ids', details)
        self.assertNotIn('children_ids', details)
        self.assertNotIn('access_token', details)

        self.authenticate('admin', 'admin')
        response = self.url_open(f'/runbot/build/{self.build.id}', headers=JSON_HEADERS)
        self.assertEqual(response.json()['access_token'], self.build.access_token)

    def test_batch_json_expands_slots(self):
        response = self.url_open(f'/runbot/batch/{self.dev_batch.id}', headers=JSON_HEADERS)
        self.assertEqual(response.status_code, 200)
        details = response.json()
        self.assertEqual(details['id'], self.dev_batch.id)
        self.assertEqual([slot['build_id']['id'] for slot in details['slot_ids']], [self.build.id])

    def test_bundle_json_field_access(self):
        response = self.url_open(f'/runbot/bundle/{self.dev_bundle.id}', headers=JSON_HEADERS)
        self.assertEqual(response.status_code, 200)
        details = response.json()
        self.assertEqual(details['bundle']['id'], self.dev_bundle.id)
        self.assertNotIn('priority', details['bundle'])
        self.assertNotIn('modules', details['bundle'])
        self.assertNotIn('team_id', details['bundle'])
        self.assertNotIn('build_all', details['batches'][0])

        self.authenticate('admin', 'admin')
        response = self.url_open(f'/runbot/bundle/{self.dev_bundle.id}', headers=JSON_HEADERS)
        self.assertIn('priority', response.json()['bundle'])

    def test_build_public_fields(self):
        public_build = self.build.with_user(self.env.ref('base.public_user'))
        self.assertEqual(public_build.global_result, 'ok')
        with self.assertRaises(AccessError):
            public_build.access_token

    def test_public_pages_render(self):
        self.dev_bundle.last_batch = self.dev_batch
        for url in [
            f'/runbot/{self.project.id}',
            f'/runbot/bundle/{self.dev_bundle.id}',
            f'/runbot/batch/{self.dev_batch.id}',
            f'/runbot/build/{self.build.id}',
            f'/runbot/branch/{self.dev_pr.id}',
            f'/runbot/commit/{self.initial_server_commit.id}',
            '/runbot/glances',
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.url_open(url).status_code, 200)

    def test_bundle_batch_public_fields(self):
        public_user = self.env.ref('base.public_user')
        public_bundle = self.dev_bundle.with_user(public_user)
        public_batch = self.dev_batch.with_user(public_user)
        self.assertEqual(public_bundle.name, self.dev_bundle.name)
        self.assertEqual(public_batch.bundle_id, self.dev_bundle)
        with self.assertRaises(AccessError):
            public_bundle.modules
        with self.assertRaises(AccessError):
            public_batch.build_all

    def test_branch_public_fields(self):
        self.dev_pr.pr_author = 'test-author'
        public_branch = self.dev_pr.with_user(self.env.ref('base.public_user'))
        self.assertEqual(public_branch.name, '1234')
        self.assertTrue(public_branch.is_pr)
        with self.assertRaises(AccessError):
            public_branch.pr_author

        response = self.url_open(f'/runbot/branch/{self.dev_pr.id}', headers=JSON_HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['id'], self.dev_pr.id)
        self.assertNotIn('pr_author', response.json())

    def test_details_include_exclude(self):
        child = self.Build.create({'params_id': self.base_params.id, 'parent_id': self.build.id})
        Build = type(self.env['runbot.build'])

        details = get_details(self.build)[0]
        self.assertIn('description', details)
        self.assertNotIn('build_time', details)
        self.assertNotIn('children_ids', details)

        with patch.object(Build, '_details_include', ('build_time', 'children_ids'), create=True), \
                patch.object(Build, '_details_exclude', ('description',), create=True):
            details = get_details(self.build)[0]
        self.assertNotIn('description', details)
        self.assertIn('build_time', details)
        self.assertEqual([build['id'] for build in details['children_ids']], [child.id])
        self.assertNotIn('children_ids', details['children_ids'][0])

    def test_remote_repo_public_fields(self):
        public_user = self.env.ref('base.public_user')
        public_remote = self.remote_odoo.with_user(public_user)
        public_repo = self.repo_odoo.with_user(public_user)
        self.assertEqual(public_remote.base_url, self.remote_odoo.base_url)
        self.assertEqual(public_repo.name, self.repo_odoo.name)
        self.assertIn(self.remote_odoo, self.env['runbot.remote'].with_user(public_user).search([]))
        self.assertIn(self.repo_odoo, self.env['runbot.repo'].with_user(public_user).search([]))
        with self.assertRaises(AccessError):
            public_remote.fetch_pull
        with self.assertRaises(AccessError):
            public_repo.mode

    def test_trigger_project_public_fields(self):
        public_user = self.env.ref('base.public_user')
        public_trigger = self.trigger_server.with_user(public_user)
        public_project = self.project.with_user(public_user)
        self.assertEqual(public_trigger.name, 'Server trigger')
        self.assertEqual(public_project.name, self.project.name)
        self.assertIn(self.trigger_server, self.env['runbot.trigger'].with_user(public_user).search([('project_id', '=', self.project.id)]))
        self.assertIn(self.project, self.env['runbot.project'].with_user(public_user).search([('hidden', '=', False)]))
        with self.assertRaises(AccessError):
            public_trigger.config_id
        with self.assertRaises(AccessError):
            public_project.keep_sticky_running

    def test_version_category_public_fields(self):
        public_user = self.env.ref('base.public_user')
        public_version = self.version_13.with_user(public_user)
        category = self.env.ref('runbot.default_category')
        self.assertEqual(public_version.name, '13.0')
        self.assertEqual(public_version.number, self.version_13.number)
        self.assertIn(self.version_13, self.env['runbot.version'].with_user(public_user).search([]))
        self.assertIn(category, self.env['runbot.category'].with_user(public_user).search([]))
        self.assertEqual(category.with_user(public_user).name, category.name)
        with self.assertRaises(AccessError):
            public_version.dockerfile_id
