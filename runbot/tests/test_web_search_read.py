import json
import logging


from odoo.tests.common import tagged, HttpCase, new_test_user
from .common import RunbotCase

_logger = logging.getLogger(__name__)


@tagged('-at_install', 'post_install')
class TestWebsearchReadAccess(HttpCase, RunbotCase):

    def setUp(self):
        super().setUp()
        create_context = {'no_reset_password': True, 'mail_create_nolog': True, 'mail_create_nosubscribe': True, 'mail_notrack': True}
        self.simple_user = new_test_user(self.env, login='simple', name='simple', password='simple', context=create_context)

        runbot_module_category = self.env['ir.module.category'].search([['name', 'ilike', 'runbot']], limit=1)

        self.other_project_group = self.env['res.groups'].create({
            'name': 'Other Project',
            'category_id': runbot_module_category.id,
        })

        self.other_project = self.env['runbot.project'].create({'name': 'Other Test Project', 'group_ids': self.other_project_group.ids})

        self.repo_server_in_other_project = self.Repo.create({
            'name': 'server',
            'project_id': self.other_project.id,
            'server_files': 'server.py',
            'addons_paths': 'addons,core/addons'
        })

        self.remote_server_other_project = self.Remote.create({
            'name': 'bla@example.com:base/server',
            'repo_id': self.repo_server_in_other_project.id,
            'token': '123',
        })

        self.initial_server_commit_other_project = self.Commit.create({
            'name': 'deadbeef',
            'repo_id': self.repo_server_in_other_project.id,
            'date': '2024-12-07',
            'subject': 'Foo bar commit',
            'author': 'r23',
            'author_email': 'r23@nowhere.com'
        })

        self.branch_server_other_project = self.Branch.create({
            'name': '17.0-other-project-feature',
            'remote_id': self.remote_server_other_project.id,
            'is_pr': False,
            'head': self.initial_server_commit_other_project.id,
        })

        self.stop_patcher('isfile')  # should not be patched during HttpCase, used in http.py

    def test_web_search_read_user_access(self):
        search_read_params = {
                'uid': 711675,
                'model': 'runbot.bundle',
                'domain': json.dumps([('id', '=', self.master_bundle.id)]),
                'specification': json.dumps({'name':{}})
            }
        res = self.url_open('http://127.0.0.1:8069/runbot/api/web_search_read', data=search_read_params)

        self.assertEqual(res.status_code, 403, 'A non existing user should get a 403')
        self.assertEqual(res.json(), {'error': 'Unauthorized'})

        search_read_params['uid'] = self.simple_user.id
        res = self.opener.post('http://127.0.0.1:8069/runbot/api/web_search_read', data=search_read_params)
        self.assertEqual(res.status_code, 403, 'A user without a token should get a 403')
        self.assertEqual(res.json(), {'error': 'Invalid user or token'})

        self.simple_user.action_generate_token()
        search_read_params['token'] = 'foobar'
        res = self.opener.post('http://127.0.0.1:8069/runbot/api/web_search_read', data=search_read_params)
        self.assertEqual(res.status_code, 403, 'A user without a token should get a 403')
        self.assertEqual(res.json(), {'error': 'Invalid user or token'})

        search_read_params['token'] = self.simple_user.runbot_api_token
        res = self.opener.post('http://127.0.0.1:8069/runbot/api/web_search_read', data=search_read_params)
        self.assertEqual(res.status_code, 200, 'A valid user with a valid token sould get a 200')

        search_read_params['specification'] =  json.dumps({'priority': {}})
        res = self.opener.post('http://127.0.0.1:8069/runbot/api/web_search_read', data=search_read_params)
        self.assertEqual(res.status_code, 403, 'A valid user without proper group should not be able to read a field with a group restriction')
        self.assertEqual(res.json(), {'error': 'Unauthorized'})

        search_read_params.update({
            'model': 'runbot.bundle',
            'domain': json.dumps([('branch_ids', 'in', self.branch_server_other_project.id)]),
            'specification': json.dumps({'name':{}}),
        })
        res = self.opener.post('http://127.0.0.1:8069/runbot/api/web_search_read', data=search_read_params)
        self.assertEqual(res.json().get('records', []), [], 'A valid user without proper group should not be able to read objects from a another project with a group restriction')
        self.assertEqual(res.status_code, 403, 'A valid user without proper group should not be able to read objects from a another project with a group restriction')
        self.assertEqual(res.json(), {'error': 'Unauthorized'})
