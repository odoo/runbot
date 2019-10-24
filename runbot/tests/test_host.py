# -*- coding: utf-8 -*-
from unittest.mock import patch
from odoo.tests import common


class TestHost(common.TransactionCase):

    @patch('odoo.addons.runbot.models.host.fqdn')
    def test_get_current(self, mock_fqdn):
        expected_name = 'runbotxxx.somewhere.com'
        mock_fqdn.return_value = expected_name
        host = self.env['runbot.host']._get_current()
        self.assertEqual(host.name, expected_name)
        self.assertEqual(host.display_name, expected_name)

    @patch('odoo.addons.runbot.models.repo.runbot_repo._git_read_gc_log')
    def test_check_repos(self, mock_read_gc_log):
        mock_read_gc_log.return_value = 'error: Could not read 2b660ddbdb4494b2637e0d2574d3fc89093d6b11'
        host = self.env['runbot.host'].create({'name': 'host_foo'})
        self.assertFalse(host.assigned_only)
        self.env['runbot.repo'].create({'name': 'bla@example.com:foo/bar'})
        with self.assertLogs(logger='odoo.addons.runbot.models.host') as assert_log:
            host._check_repos()
            self.assertTrue(host.assigned_only)
            self.assertIn('gc.log file found in repo', assert_log.output[0])
            self.assertIn('error: Could not read', host.last_exception)
