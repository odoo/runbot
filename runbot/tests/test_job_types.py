
# -*- coding: utf-8 -*-
from time import localtime
from unittest.mock import patch
from odoo.tools.misc import DEFAULT_SERVER_DATETIME_FORMAT
from odoo.tests import common


class Test_Jobs(common.TransactionCase):


    def setUp(self):
        super(Test_Jobs, self).setUp()
        self.Repo = self.env['runbot.repo']
        self.repo = self.Repo.create({'name': 'bla@example.com:foo/bar', 'token': 'xxx'})
        self.Branch = self.env['runbot.branch']
        self.branch_master = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/heads/master',
        })
        self.Build = self.env['runbot.build']
        self.build = self.Build.create({
            'branch_id': self.branch_master.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            'port' : '1234',
        })

    @patch('odoo.addons.runbot.models.build.docker_run')
    @patch('odoo.addons.runbot.models.build.runbot_build._local_pg_createdb')
    def test_job_10(self, mock_create_db, mock_docker_run):
        """ Test that job10 is done or skipped depending on job_type """
        # test that the default job_type value executes the tests
        mock_docker_run.return_value = "Mocked run"
        ret = self.Build._job_10_test_base(self.build, '/tmp/x.log')
        self.assertEqual("Mocked run", ret, "A build with default job_type should run job_10")

        # test skip when job_type is none
        self.build.job_type = 'none'
        ret = self.Build._job_10_test_base(self.build, '/tmp/x.log')
        self.assertEqual(-2, ret, "A build with job_type 'none' should skip job_10")

        # test skip when job_type is running
        self.build.job_type = 'running'
        ret = self.Build._job_10_test_base(self.build, '/tmp/x.log')
        self.assertEqual(-2, ret, "A build with job_type 'running' should skip job_10")

        # test run when job_type is testing
        self.build.job_type = 'testing'
        ret = self.Build._job_10_test_base(self.build, '/tmp/x.log')
        self.assertEqual("Mocked run", ret, "A build with job_type 'testing' should run job_10")

        # test run when job_type is all
        self.build.job_type = 'all'
        ret = self.Build._job_10_test_base(self.build, '/tmp/x.log')
        self.assertEqual("Mocked run", ret, "A build with job_type 'all' should run job_10")