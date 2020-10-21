# -*- coding: utf-8 -*-
import datetime
from unittest.mock import patch
from werkzeug.urls import url_parse

from odoo.tests.common import HttpCase, new_test_user, tagged
from odoo.tools import mute_logger


@tagged('post_install', '-at_install')
class TestCommitStatus(HttpCase):

    def setUp(self):
        super(TestCommitStatus, self).setUp()
        self.project = self.env['runbot.project'].create({'name': 'Tests'})
        self.repo_server = self.env['runbot.repo'].create({
            'name': 'server',
            'project_id': self.project.id,
            'server_files': 'server.py',
            'addons_paths': 'addons,core/addons'
        })

        self.server_commit = self.env['runbot.commit'].create({
            'name': 'dfdfcfcf0000ffffffffffffffffffffffffffff',
            'repo_id': self.repo_server.id
        })

        create_context = {'no_reset_password': True, 'mail_create_nolog': True, 'mail_create_nosubscribe': True, 'mail_notrack': True}
        with mute_logger('odoo.addons.base.models.ir_attachment'):
            self.simple_user = new_test_user(self.env, login='simple', name='simple', password='simple', context=create_context)
            self.runbot_admin = new_test_user(self.env, groups='runbot.group_runbot_admin,base.group_user', login='runbot_admin', name='runbot_admin', password='admin', context=create_context)

    def test_commit_status_resend(self):
        """test commit status resend"""

        with mute_logger('odoo.addons.http_routing.models.ir_http'), mute_logger('odoo.addons.base.models.ir_attachment'):
            commit_status = self.env['runbot.commit.status'].create({
                'commit_id': self.server_commit.id,
                'context': 'ci/test',
                'state': 'failure',
                'target_url': 'https://www.somewhere.com',
                'description': 'test status'
            })

            # 1. test that unauthenticated users are redirected to the login page
            response = self.url_open('/runbot/commit/resend/%s' % commit_status.id)
            parsed_response = url_parse(response.url)
            self.assertIn('redirect=', parsed_response.query)
            self.assertEqual(parsed_response.path, '/web/login')

            # 2. test that a simple Odoo user cannot resend a status
            # removed since the 'runbot.group_user' has been given to the 'base.group_user'.
            # self.assertEqual(response.status_code, 403)

            # 3. test that a non-existsing commit_status returns a 404
            # 3.1 find a non existing commit status id
            non_existing_id = self.env['runbot.commit.status'].browse(50000).exists() or 50000
            while self.env['runbot.commit.status'].browse(non_existing_id).exists():
                non_existing_id += 1

            self.authenticate('runbot_admin', 'admin')
            response = self.url_open('/runbot/commit/resend/%s' % non_existing_id)
            self.assertEqual(response.status_code, 404)

            #4.1 Test that a status not sent (with not sent_date) can be manually resend
            with patch('odoo.addons.runbot.models.commit.CommitStatus._send') as send_patcher:
                response = self.url_open('/runbot/commit/resend/%s' % commit_status.id)
                self.assertEqual(response.status_code, 200)
                send_patcher.assert_called()

            commit_status = self.env['runbot.commit.status'].search([], order='id desc', limit=1)
            self.assertEqual(commit_status.description, 'Status resent by runbot_admin')

            # 4.2 Finally test that a new status is created on resend and that the _send method is called
            with patch('odoo.addons.runbot.models.commit.CommitStatus._send') as send_patcher:
                a_minute_ago = datetime.datetime.now() - datetime.timedelta(seconds=65)
                commit_status.sent_date = a_minute_ago
                response = self.url_open('/runbot/commit/resend/%s' % commit_status.id)
                self.assertEqual(response.status_code, 200)
                send_patcher.assert_called()

            last_commit_status = self.env['runbot.commit.status'].search([], order='id desc', limit=1)
            self.assertEqual(last_commit_status.description, 'Status resent by runbot_admin')

            # 5. Now that the a new status was created, status is not the last one and thus, cannot be resent
            response = self.url_open('/runbot/commit/resend/%s' % commit_status.id)
            self.assertEqual(response.status_code, 403)

            # 6. try to immediately resend the commit should fail to avoid spamming github
            last_commit_status.sent_date = datetime.datetime.now()  # as _send is mocked, the sent_date is not set
            with patch('odoo.addons.runbot.models.commit.CommitStatus._send') as send_patcher:
                response = self.url_open('/runbot/commit/resend/%s' % last_commit_status.id)
                self.assertEqual(response.status_code, 200)
                send_patcher.assert_not_called()
