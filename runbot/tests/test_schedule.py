# -*- coding: utf-8 -*-
import datetime
from unittest.mock import patch
from odoo.tests import common
import odoo


class TestSchedule(common.TransactionCase):

    def setUp(self):
        # entering test mode to avoid that the _schedule method commits records
        registry = odoo.registry()
        registry.enter_test_mode()
        self.addCleanup(registry.leave_test_mode)
        super(TestSchedule, self).setUp()
        self.Repo = self.env['runbot.repo']
        self.repo = self.Repo.create({'name': 'bla@example.com:foo/bar'})
        self.Branch = self.env['runbot.branch']
        self.branch = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/heads/master'
        })
        self.Build = self.env['runbot.build']

    @patch('odoo.addons.runbot.models.build.os.makedirs')
    @patch('odoo.addons.runbot.models.build.os.path.getmtime')
    @patch('odoo.addons.runbot.models.build.docker_is_running')
    def test_schedule_mark_done(self, mock_running, mock_getmtime, mock_makedirs):
        """ Test that results are set even when job_30_run is skipped """
        job_end_time = datetime.datetime.now()
        mock_getmtime.return_value = job_end_time.timestamp()

        build = self.Build.create({
            'local_state': 'testing',
            'branch_id': self.branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            'port': '1234',
            'host': 'runbotxx',
            'job_start': datetime.datetime.now(),
            'config_id': self.env.ref('runbot.runbot_build_config_default').id,
            'active_step': self.env.ref('runbot.runbot_build_config_step_run').id,
        })
        domain = [('repo_id', 'in', (self.repo.id, ))]
        domain_host = domain + [('host', '=', 'runbotxx')]
        build_ids = self.Build.search(domain_host + [('local_state', 'in', ['testing', 'running', 'deathrow'])])
        mock_running.return_value = False
        self.assertEqual(build.local_state, 'testing')
        build_ids._schedule()  # too fast, docker not started
        self.assertEqual(build.local_state, 'testing')
        build_ids.write({'job_start': datetime.datetime.now() - datetime.timedelta(seconds=20)})  # job is now a little older
        build_ids._schedule()
        self.assertEqual(build.local_state, 'done')
        self.assertEqual(build.local_result, 'ok')
