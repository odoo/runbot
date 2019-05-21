# -*- coding: utf-8 -*-
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
