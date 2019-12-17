# -*- coding: utf-8 -*-
from unittest.mock import patch
from odoo.tests import common
from .common import RunbotCase


class Test_Cron(RunbotCase):

    def setUp(self):
        super(Test_Cron, self).setUp()
        self.start_patcher('_get_cron_period', 'odoo.addons.runbot.models.repo.runbot_repo._get_cron_period', 2)

    @patch('odoo.addons.runbot.models.repo.config.get')
    def test_cron_period(self, mock_config_get):
        """ Test that the random cron period stays below margin
        Assuming a configuration of 10 minutes cron limit
        """
        mock_config_get.return_value = 600
        period = self.Repo._get_cron_period(min_margin=200)
        for i in range(200):
            self.assertLess(period, 400)

    def test_crons_returns(self):
        """ test that cron_fetch_and_schedule and _cron_fetch_and_build
        return directly when called on wrong host
        """

        ret = self.Repo._cron_fetch_and_schedule('runbotx.foo.com')
        self.assertEqual(ret, 'Not for me')

        ret = self.Repo._cron_fetch_and_build('runbotx.foo.com')
        self.assertEqual(ret, 'Not for me')

    @patch('odoo.addons.runbot.models.repo.runbot_repo._create_pending_builds')
    @patch('odoo.addons.runbot.models.repo.runbot_repo._update')
    def test_cron_schedule(self, mock_update, mock_create):
        """ test that cron_fetch_and_schedule do its work """
        self.env['ir.config_parameter'].sudo().set_param('runbot.runbot_update_frequency', 1)
        self.Repo.create({'name': '/path/somewhere/disabled.git', 'mode': 'disabled'})  # create a disabled
        self.Repo.search([]).write({'mode': 'disabled'}) #  disable all depo, in case we have existing ones
        local_repo = self.Repo.create({'name': '/path/somewhere/rep.git'})  # create active repo
        ret = self.Repo._cron_fetch_and_schedule('host.runbot.com')
        self.assertEqual(None, ret)
        mock_update.assert_called_with(force=False)
        mock_create.assert_called_with()

    @patch('odoo.addons.runbot.models.repo.runbot_repo._reload_nginx')
    @patch('odoo.addons.runbot.models.repo.runbot_repo._scheduler')
    def test_cron_build(self, mock_scheduler, mock_reload):
        """ test that cron_fetch_and_build do its work """
        hostname = 'host.runbot.com'
        self.env['ir.config_parameter'].sudo().set_param('runbot.runbot_update_frequency', 1)
        self.Repo.create({'name': '/path/somewhere/disabled.git', 'mode': 'disabled'})  # create a disabled
        self.Repo.search([]).write({'mode': 'disabled'}) #  disable all depo, in case we have existing ones
        local_repo = self.Repo.create({'name': '/path/somewhere/rep.git'})  # create active repo
        ret = self.Repo._cron_fetch_and_build(hostname)
        self.assertEqual(None, ret)
        mock_scheduler.assert_called()
        host = self.env['runbot.host'].search([('name', '=', hostname)])
        self.assertEqual(host.name, hostname, 'A new host should have been created')
        self.assertGreater(host.psql_conn_count, 0, 'A least one connection should exist on the current psql instance')
