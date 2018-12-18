# -*- coding: utf-8 -*-
from unittest.mock import patch
from odoo.tests import common

class Test_Branch(common.TransactionCase):

    def setUp(self):
        super(Test_Branch, self).setUp()
        Repo = self.env['runbot.repo']
        self.repo = Repo.create({'name': 'bla@example.com:foo/bar', 'token': '123'})
        self.Branch = self.env['runbot.branch']

        #mock_patch = patch('odoo.addons.runbot.models.repo.runbot_repo._github', self._github)
        #mock_patch.start()
        #self.addCleanup(mock_patch.stop)

    def test_base_fields(self):
        branch = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/head/master'
        })

        self.assertEqual(branch.branch_name, 'master')
        self.assertEqual(branch.branch_url, 'https://example.com/foo/bar/tree/master')
        self.assertEqual(branch.job_type, 'all')

    @patch('odoo.addons.runbot.models.repo.runbot_repo._github')
    def test_pull_request(self, mock_github):
        mock_github.return_value = {
            'head' : {'label': 'foo-dev:bar_branch'},
            'base' : {'ref': 'master'},
        }
        pr = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/pull/12345'
        })
        self.assertEqual(pr.branch_name, '12345')
        self.assertEqual(pr.branch_url, 'https://example.com/foo/bar/pull/12345')
        self.assertEqual(pr.target_branch_name, 'master')
        self.assertEqual(pr.pull_head_name, 'foo-dev:bar_branch')

    def test_coverage_in_name(self):
        """Test that coverage in branch name enables coverage"""
        branch = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/head/foo-branch-bar'
        })
        self.assertFalse(branch.coverage)
        cov_branch = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/head/foo-coverage-branch-bar'
        })
        self.assertTrue(cov_branch.coverage)
