# -*- coding: utf-8 -*-
from unittest.mock import patch
from odoo.tests import common
from .common import RunbotCase

class Test_Branch(RunbotCase):

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
        self.assertEqual(branch.config_id, self.env.ref('runbot.runbot_build_config_default'))

    def test_pull_request(self):
        mock_github = self.patchers['github_patcher']
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
        self.assertEqual(branch.config_id, self.env.ref('runbot.runbot_build_config_default'))
        cov_branch = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/head/foo-use-coverage-branch-bar'
        })
        self.assertEqual(cov_branch.config_id, self.env.ref('runbot.runbot_build_config_test_coverage'))


class TestBranchRelations(RunbotCase):

    def setUp(self):
        super(TestBranchRelations, self).setUp()

        self.repo = self.env['runbot.repo'].create({'name': 'bla@example.com:foo/bar'})
        self.repodev = self.env['runbot.repo'].create({'name': 'bla@example.com:foo-dev/bar', 'duplicate_id':self.repo.id })
        self.Branch = self.env['runbot.branch']

        def create_sticky(name):
            return self.Branch.create({
                'repo_id': self.repo.id,
                'name': 'refs/heads/%s' % name,
                'sticky': True
            })
        self.master = create_sticky('master')
        create_sticky('11.0')
        create_sticky('saas-11.1')
        create_sticky('12.0')
        create_sticky('saas-12.3')
        create_sticky('13.0')
        create_sticky('saas-13.1')
        self.last = create_sticky('saas-13.2')

    def test_relations_master_dev(self):
        b = self.Branch.create({
                'repo_id': self.repodev.id,
                'name': 'refs/heads/master-test-tri',
            })
        self.assertEqual(b.closest_sticky.branch_name, 'master')
        self.assertEqual(b.previous_version.branch_name, '13.0')
        self.assertEqual(sorted(b.intermediate_stickies.mapped('branch_name')), ['saas-13.1', 'saas-13.2'])

    def test_relations_master(self):
        b = self.master
        self.assertEqual(b.closest_sticky.branch_name, 'master')
        self.assertEqual(b.previous_version.branch_name, '13.0')
        self.assertEqual(sorted(b.intermediate_stickies.mapped('branch_name')), ['saas-13.1', 'saas-13.2'])

    def test_relations_no_intermediate(self):
        b = self.Branch.create({
                'repo_id': self.repodev.id,
                'name': 'refs/heads/saas-13.1-test-tri',
            })
        self.assertEqual(b.closest_sticky.branch_name, 'saas-13.1')
        self.assertEqual(b.previous_version.branch_name, '13.0')
        self.assertEqual(sorted(b.intermediate_stickies.mapped('branch_name')), [])

    def test_relations_old_branch(self):
        b = self.Branch.create({
                'repo_id': self.repodev.id,
                'name': 'refs/heads/11.0-test-tri',
            })
        self.assertEqual(b.closest_sticky.branch_name, '11.0')
        self.assertEqual(b.previous_version.branch_name, False)
        self.assertEqual(sorted(b.intermediate_stickies.mapped('branch_name')), [])

    def test_relations_closest_forced(self):
        b = self.Branch.create({
                'repo_id': self.repodev.id,
                'name': 'refs/heads/master-test-tri',
            })
        self.assertEqual(b.closest_sticky.branch_name, 'master')
        self.assertEqual(b.previous_version.branch_name, '13.0')
        self.assertEqual(sorted(b.intermediate_stickies.mapped('branch_name')), ['saas-13.1', 'saas-13.2'])

        b.closest_sticky = self.last

        self.assertEqual(b.closest_sticky.branch_name, 'saas-13.2')
        self.assertEqual(b.previous_version.branch_name, '13.0')
        self.assertEqual(sorted(b.intermediate_stickies.mapped('branch_name')), ['saas-13.1'])

    def test_relations_no_match(self):
        b = self.Branch.create({
                'repo_id': self.repodev.id,
                'name': 'refs/heads/icantnamemybranches',
            })

        self.assertEqual(b.closest_sticky.branch_name, False)
        self.assertEqual(b.previous_version.branch_name, False)
        self.assertEqual(sorted(b.intermediate_stickies.mapped('branch_name')), [])

    def test_relations_pr(self):
        self.Branch.create({
                'repo_id': self.repodev.id,
                'name': 'refs/heads/master-test-tri',
            })
        b = self.Branch.create({
                'repo_id': self.repodev.id,
                'target_branch_name': 'master-test-tri',
                'name': 'refs/pull/100',
            })
        b.target_branch_name = 'master-test-tri'
        self.assertEqual(b.closest_sticky.branch_name, 'master')
        self.assertEqual(b.previous_version.branch_name, '13.0')
        self.assertEqual(sorted(b.intermediate_stickies.mapped('branch_name')), ['saas-13.1', 'saas-13.2'])


