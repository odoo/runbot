# -*- coding: utf-8 -*-
from unittest.mock import patch
from odoo.tests import common


class TestBuildConfigStep(common.TransactionCase):

    def setUp(self):
        super(TestBuildConfigStep, self).setUp()
        self.Repo = self.env['runbot.repo']
        self.repo = self.Repo.create({'name': 'bla@example.com:foo/bar'})
        self.Branch = self.env['runbot.branch']
        self.branch = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/heads/master'
        })
        self.branch_10 = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/heads/10.0'
        })
        self.branch_11 = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/heads/11.0'
        })
        self.Build = self.env['runbot.build']
        self.ConfigStep = self.env['runbot.build.config.step']
        self.Config = self.env['runbot.build.config']

        self.parent_build = self.Build.create({
            'branch_id': self.branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            'port': '1234',
        })

    def test_config_step_create_results(self):
        """ Test child builds are taken into account"""

        config_step = self.ConfigStep.create({
            'name': 'test_step',
            'job_type': 'create_build',
            'number_builds': 2,
            'make_orphan': False,
            'force_build': True,
        })

        config = self.Config.create({'name': 'test_config'})
        config_step.create_config_ids = [config.id]

        config_step._create_build(self.parent_build, '/tmp/essai')
        self.assertEqual(len(self.parent_build.children_ids), 2, 'Two sub-builds should have been generated')

        # check that the result will be ignored by parent build
        for child_build in self.parent_build.children_ids:
            self.assertFalse(child_build.orphan_result)
            child_build.local_result = 'ko'
            self.assertEqual(child_build.global_result, 'ko')

        self.assertEqual(self.parent_build.global_result, 'ko')

    def test_config_step_create(self):
        """ Test the config step of type create """

        config_step = self.ConfigStep.create({
            'name': 'test_step',
            'job_type': 'create_build',
            'number_builds': 2,
            'make_orphan': True,
            'force_build': True,
        })

        config = self.Config.create({'name': 'test_config'})
        config_step.create_config_ids = [config.id]

        config_step._create_build(self.parent_build, '/tmp/essai')
        self.assertEqual(len(self.parent_build.children_ids), 2, 'Two sub-builds should have been generated')

        # check that the result will be ignored by parent build
        for child_build in self.parent_build.children_ids:
            self.assertTrue(child_build.orphan_result, 'An orphan result config step should mark the build as orphan_result')
            child_build.local_result = 'ko'

        self.assertFalse(self.parent_build.global_result)


    @patch('odoo.addons.runbot.models.build.runbot_build._local_pg_createdb')
    @patch('odoo.addons.runbot.models.build.runbot_build._get_server_info')
    @patch('odoo.addons.runbot.models.build.runbot_build._get_addons_path')
    @patch('odoo.addons.runbot.models.build.runbot_build._get_py_version')
    @patch('odoo.addons.runbot.models.build.runbot_build._server')
    @patch('odoo.addons.runbot.models.build.runbot_build._checkout')
    @patch('odoo.addons.runbot.models.build_config.docker_run')
    def test_coverage(self, mock_docker_run, mock_checkout, mock_server, mock_get_py_version, mock_get_addons_path, mock_get_server_info, mock_local_pg_createdb):
        config_step = self.ConfigStep.create({
            'name': 'coverage',
            'job_type': 'install_odoo',
            'coverage': True
        })

        mock_checkout.return_value = {}
        mock_server.return_value = 'bar'
        mock_get_py_version.return_value = '3'
        mock_get_addons_path.return_value = ['bar/addons']
        mock_get_server_info.return_value = (self.parent_build._get_all_commit()[0], 'server.py')
        mock_local_pg_createdb.return_value = True

        def docker_run(cmd, log_path, build_dir, *args, **kwargs):
            cmds = cmd.split(' && ')
            self.assertEqual(cmds[0], 'sudo pip3 install -r bar/requirements.txt')
            self.assertEqual(cmds[1].split(' bar/server.py')[0], 'python3 -m coverage run --branch --source /data/build --omit *__manifest__.py')
            self.assertEqual(cmds[2], 'python3 -m coverage html -d /data/build/coverage --ignore-errors')
            self.assertEqual(log_path, 'dev/null/logpath')

        mock_docker_run.side_effect = docker_run
    
        config_step._run_odoo_install(self.parent_build, 'dev/null/logpath')
