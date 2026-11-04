from odoo.tests import HttpCase, tagged

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
        batch = self.get_json(f'/runbot/api/batch/{self.dev_batch.id}')[0]
        self.assertEqual([slot['build']['id'] for slot in batch['slots'] if slot['build']], self.build.ids)
        self.assertEqual(self.get_json(f'/runbot/api/build/{self.build.id}?logs=all')[0]['id'], self.build.id)
