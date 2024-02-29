import json
import logging

from unittest.mock import patch, mock_open

from odoo.tests.common import Opener, tagged, HttpCase, new_test_user
from .common import RunbotCase

_logger = logging.getLogger(__name__)


@tagged('-at_install', 'post_install')
class TestWebsearchReadAccess(RunbotCase, HttpCase):

    def setUp(self):
        create_context = {'no_reset_password': True, 'mail_create_nolog': True, 'mail_create_nosubscribe': True, 'mail_notrack': True}
        self.simple_user = new_test_user(self.env, login='simple', name='simple', password='simple', context=create_context)

    def test_user_token(self):
        poster = Opener(self.cr)
        res = poster.post(
            'http://127.0.0.1:8069/runbot/api/web_search_read',
            data={
                'uid': 711675,
                'domain': json.dumps([('id', '=', 1)]),
                'model': 'runbot.bundle',
                'specification': json.dumps({'name':{}})
            }
        )

        self.assertEqual(res.status_code, 403, 'A non existing user should get a 403')
        self.assertEqual(res.json(), {'error': 'Unauthorized'})
