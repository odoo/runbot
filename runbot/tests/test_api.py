from odoo.tests import HttpCase, new_test_user, tagged

from .common import RunbotCase


@tagged('-at_install', 'post_install')
class TestRunbotApi(RunbotCase, HttpCase):

    def setUp(self):
        super().setUp()
        self.project.group_ids = False
        self.build = self.Build.create({'params_id': self.base_params.id})
        self.env['runbot.batch.slot'].create({
            'batch_id': self.dev_batch.id,
            'trigger_id': self.trigger_server.id,
            'build_id': self.build.id,
            'params_id': self.base_params.id,
            'link_type': 'created',
        })

    def get_json(self, url):
        response = self.url_open(url)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_api(self):
        self.assertIn('/runbot/api/build/', self.url_open('/runbot/api').text)
        for url in ('/runbot/api/bundle/master-dev-tri', f'/runbot/api/bundle/{self.dev_bundle.id}', '/runbot/api/bundle?pr=odoo:1234'):
            with self.subTest(url=url):
                self.assertEqual([bundle['id'] for bundle in self.get_json(url)], self.dev_bundle.ids)
        self.dev_bundle.last_batch = self.dev_batch
        bundle = self.get_json(f'/runbot/api/bundle/{self.dev_bundle.id}')[0]
        self.assertNotIn('batches', bundle)
        self.assertEqual(bundle['last_batch']['id'], self.dev_batch.id)
        self.assertEqual(bundle['last_batch']['global_state'], 'pending')
        bundle = self.get_json(f'/runbot/api/bundle/{self.dev_bundle.id}?batch_history=100')[0]
        self.assertEqual([batch['id'] for batch in bundle['batches']], self.dev_batch.ids)
        batch = self.get_json(f'/runbot/api/batch/{self.dev_batch.id}')[0]
        self.assertEqual([slot['build']['id'] for slot in batch['slots'] if slot['build']], self.build.ids)
        commits = self.get_json(f'/runbot/api/commit/{self.initial_server_commit.name},{self.initial_server_commit.id}')
        self.assertEqual([commit['id'] for commit in commits], self.initial_server_commit.ids)
        self.assertEqual(self.get_json(f'/runbot/api/build/{self.build.id}?logs=all')[0]['id'], self.build.id)

    def test_api_errors(self):
        error_content = self.env['runbot.build.error.content'].create({'content': 'foo bar', 'project_id': self.project.id})
        error = error_content.error_id
        builds = self.Build.create([{'params_id': self.base_params.id}, {'params_id': self.base_params.id}])
        self.env['runbot.build.error.link'].create([{'build_id': build.id, 'error_content_id': error_content.id} for build in builds])
        # visible without login when the project requires no group
        self.assertIn(error.id, [error['id'] for error in self.get_json('/runbot/api/errors?sort=build_count desc')])
        error_data = self.get_json(f'/runbot/api/error/{error.id}')[0]
        self.assertEqual(error_data['build_count'], 2)
        self.assertEqual([content['id'] for content in error_data['contents']], error_content.ids)
        self.assertEqual(self.url_open('/runbot/api/errors?limit=51').status_code, 400)

        self.project.group_ids = self.env.ref('runbot.group_user')
        self.assertEqual(self.get_json(f'/runbot/api/error/{error.id}'), [])
        # os.path patches break the filestore (user avatar) and the http sessions
        for patcher_name in ('isfile', 'isdir', 'file_exist'):
            self.stop_patcher(patcher_name)
        new_test_user(self.env, login='api_user', groups='base.group_user', password='api_user')
        self.authenticate('api_user', 'api_user')
        self.assertEqual([error['id'] for error in self.get_json(f'/runbot/api/error/{error.id}')], error.ids)
