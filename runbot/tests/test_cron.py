# -*- coding: utf-8 -*-
from unittest.mock import patch
from odoo.tests import common


class Test_Cron(common.TransactionCase):

    def setUp(self):
        super(Test_Cron, self).setUp()
        self.Repo = self.env['runbot.repo']

    @patch('odoo.addons.runbot.models.repo.config.get')
    def test_cron_period(self, mock_config_get):
        """ Test that the random cron period stays below margin
        Assuming a configuration of 10 minutes cron limit
        """
        mock_config_get.return_value = 600
        period = self.Repo._get_cron_period(min_margin=200)
        for i in range(200):
            self.assertLess(period, 400)

    @patch('odoo.addons.runbot.models.repo.fqdn')
    def test_crons_returns(self, mock_fqdn):
        """ test that cron_fetch_and_schedule and _cron_fetch_and_build
        return directly when called on wrong host
        """
        mock_fqdn.return_value = 'runboty.foo.com'
        ret = self.Repo._cron_fetch_and_schedule('runbotx.foo.com')
        self.assertEqual(ret, 'Not for me')

        ret = self.Repo._cron_fetch_and_build('runbotx.foo.com')
        self.assertEqual(ret, 'Not for me')

    @patch('odoo.addons.runbot.models.repo.runbot_repo._get_cron_period')
    @patch('odoo.addons.runbot.models.repo.runbot_repo._create_pending_builds')
    @patch('odoo.addons.runbot.models.repo.runbot_repo._update')
    @patch('odoo.addons.runbot.models.repo.fqdn')
    def test_cron_schedule(self, mock_fqdn, mock_update, mock_create, mock_cron_period):
        """ test that cron_fetch_and_schedule do its work """
        mock_fqdn.return_value = 'runbotx.foo.com'
        mock_cron_period.return_value = 2
        self.env['ir.config_parameter'].sudo().set_param('runbot.runbot_update_frequency', 1)
        self.Repo.create({'name': '/path/somewhere/disabled.git', 'mode': 'disabled'})  # create a disabled
        self.Repo.search([]).write({'mode': 'disabled'}) #  disable all depo, in case we have existing ones
        local_repo = self.Repo.create({'name': '/path/somewhere/rep.git'})  # create active repo
        ret = self.Repo._cron_fetch_and_schedule('runbotx.foo.com')
        self.assertEqual(None, ret)
        mock_update.assert_called_with(force=False)
        mock_create.assert_called_with()

    @patch('odoo.addons.runbot.models.host.fqdn')
    @patch('odoo.addons.runbot.models.repo.runbot_repo._get_cron_period')
    @patch('odoo.addons.runbot.models.repo.runbot_repo._reload_nginx')
    @patch('odoo.addons.runbot.models.repo.runbot_repo._scheduler')
    @patch('odoo.addons.runbot.models.repo.fqdn')
    def test_cron_build(self, mock_fqdn, mock_scheduler, mock_reload, mock_cron_period, mock_host_fqdn):
        """ test that cron_fetch_and_build do its work """
        hostname = 'runbotx.foo.com'
        mock_fqdn.return_value = mock_host_fqdn.return_value = hostname
        mock_cron_period.return_value = 2
        self.env['ir.config_parameter'].sudo().set_param('runbot.runbot_update_frequency', 1)
        self.Repo.create({'name': '/path/somewhere/disabled.git', 'mode': 'disabled'})  # create a disabled
        self.Repo.search([]).write({'mode': 'disabled'}) #  disable all depo, in case we have existing ones
        local_repo = self.Repo.create({'name': '/path/somewhere/rep.git'})  # create active repo
        ret = self.Repo._cron_fetch_and_build('runbotx.foo.com')
        self.assertEqual(None, ret)
        mock_scheduler.assert_called()
        self.assertTrue(mock_reload.called)
        host = self.env['runbot.host'].search([('name', '=', 'runbotx.foo.com')])
        self.assertEqual(host.name, hostname, 'A new host should have been created')
        self.assertGreater(host.psql_conn_count, 0, 'A least one connection should exist on the current psql instance')
