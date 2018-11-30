# -*- coding: utf-8 -*-
from unittest.mock import patch
from odoo.tests import common

class Test_Build(common.TransactionCase):

    def setUp(self):
        super(Test_Build, self).setUp()
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

    @patch('odoo.addons.runbot.models.build.fqdn')
    def test_base_fields(self, mock_fqdn):
        build = self.Build.create({
            'branch_id': self.branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            'port' : '1234',
        })
        self.assertEqual(build.id, build.sequence)
        self.assertEqual(build.dest, '%05d-master-d0d0ca' % build.id)
        # test dest change on new commit
        build.name = 'deadbeef0000ffffffffffffffffffffffffffff'
        self.assertEqual(build.dest, '%05d-master-deadbe' % build.id)

        # Test domain compute with fqdn and ir.config_parameter
        mock_fqdn.return_value = 'runbot98.nowhere.org'
        self.assertEqual(build.domain, 'runbot98.nowhere.org:1234')
        self.env['ir.config_parameter'].set_param('runbot.runbot_domain', 'runbot99.example.org')
        build._get_domain()
        self.assertEqual(build.domain, 'runbot99.example.org:1234')

    def test_pr_is_duplicate(self):
        """ test PR is a duplicate of a dev branch build """
        dup_repo = self.Repo.create({
            'name': 'bla@example.com:foo-dev/bar',
            'duplicate_id': self.repo.id
        })
        self.repo.duplicate_id = dup_repo.id
        dev_branch = self.Branch.create({
            'repo_id': dup_repo.id,
            'name': 'refs/heads/10.0-fix-thing-moc'
        })
        dev_build = self.Build.create({
            'branch_id': dev_branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        pr = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/pull/12345'
        })

        pr_build = self.Build.create({
            'branch_id': pr.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        self.assertEqual(pr_build.state, 'duplicate')
        self.assertEqual(pr_build.duplicate_id.id, dev_build.id)

    def test_dev_is_duplicate(self):
        """ test dev branch build is a duplicate of a PR """
        dup_repo = self.Repo.create({
            'name': 'bla@example.com:foo-dev/bar',
            'duplicate_id': self.repo.id
        })
        self.repo.duplicate_id = dup_repo.id
        dev_branch = self.Branch.create({
            'repo_id': dup_repo.id,
            'name': 'refs/heads/10.0-fix-thing-moc'
        })
        pr = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/pull/12345'
        })

        pr_build = self.Build.create({
            'branch_id': pr.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        dev_build = self.Build.create({
            'branch_id': dev_branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        self.assertEqual(dev_build.state, 'duplicate')
        self.assertEqual(dev_build.duplicate_id.id, pr_build.id)

    @patch('odoo.addons.runbot.models.branch.runbot_branch._is_on_remote')
    def test_closest_branch_01(self, mock_is_on_remote):
        """ test find a matching branch in a target repo based on branch name """
        mock_is_on_remote.return_value = True
        server_repo = self.Repo.create({'name': 'bla@example.com:foo-dev/bar'})
        addons_repo = self.Repo.create({'name': 'bla@example.com:ent-dev/bar'})
        self.Branch.create({
            'repo_id': server_repo.id,
            'name': 'refs/heads/10.0-fix-thing-moc'
        })
        addons_branch = self.Branch.create({
            'repo_id': addons_repo.id,
            'name': 'refs/heads/10.0-fix-thing-moc'
        })
        addons_build = self.Build.create({
            'branch_id': addons_branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        self.assertEqual((server_repo.id, addons_branch.name, 'exact'), addons_build._get_closest_branch_name(server_repo.id))

    @patch('odoo.addons.runbot.models.repo.runbot_repo._github')
    def test_closest_branch_02(self, mock_github):
        """ test find two matching PR having the same head name """
        mock_github.return_value = {
            'head' : {'label': 'foo-dev:bar_branch'},
            'base' : {'ref': 'master'},
            'state': 'open'
        }
        server_repo = self.Repo.create({'name': 'bla@example.com:foo-dev/bar', 'token':  '1'})
        addons_repo = self.Repo.create({'name': 'bla@example.com:ent-dev/bar', 'token': '1'})
        server_pr = self.Branch.create({
            'repo_id': server_repo.id,
            'name': 'refs/pull/123456'
        })
        addons_pr = self.Branch.create({
            'repo_id': addons_repo.id,
            'name': 'refs/pull/789101'
        })
        addons_build = self.Build.create({
            'branch_id': addons_pr.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        self.assertEqual((server_repo.id, server_pr.name, 'exact'), addons_build._get_closest_branch_name(server_repo.id))

    @patch('odoo.addons.runbot.models.build.runbot_build._branch_exists')
    def test_closest_branch_03(self, mock_branch_exists):
        """ test find a branch based on dashed prefix"""
        mock_branch_exists.return_value = True
        addons_repo = self.Repo.create({'name': 'bla@example.com:ent-dev/bar', 'token': '1'})
        addons_branch = self.Branch.create({
            'repo_id': addons_repo.id,
            'name': 'refs/heads/10.0-fix-blah-blah-moc'
        })
        addons_build = self.Build.create({
            'branch_id': addons_branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        self.assertEqual((self.repo.id, 'refs/heads/10.0', 'prefix'), addons_build._get_closest_branch_name(self.repo.id))

    @patch('odoo.addons.runbot.models.repo.runbot_repo._github')
    def test_closest_branch_05(self, mock_github):
        """ test last resort value """
        mock_github.return_value = {
            'head' : {'label': 'foo-dev:bar_branch'},
            'base' : {'ref': '10.0'},
            'state': 'open'
        }
        server_repo = self.Repo.create({'name': 'bla@example.com:foo-dev/bar', 'token':  '1'})
        addons_repo = self.Repo.create({'name': 'bla@example.com:ent-dev/bar', 'token': '1'})
        server_pr = self.Branch.create({
            'repo_id': server_repo.id,
            'name': 'refs/pull/123456'
        })
        mock_github.return_value = {
            'head' : {'label': 'foo-dev:foobar_branch'},
            'base' : {'ref': '10.0'},
            'state': 'open'
        }
        addons_pr = self.Branch.create({
            'repo_id': addons_repo.id,
            'name': 'refs/pull/789101'
        })
        addons_build = self.Build.create({
            'branch_id': addons_pr.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        self.assertEqual((server_repo.id, server_pr.target_branch_name, 'default'), addons_build._get_closest_branch_name(server_repo.id))

@patch('odoo.addons.runbot.models.repo.runbot_repo._github')
def test_closest_branch_05_master(self, mock_github):
    """ test last resort value when nothing common can be found"""
    mock_github.return_value = {
        'head' : {'label': 'foo-dev:bar_branch'},
        'base' : {'ref': 'saas-14'},
        'state': 'open'
    }
    server_repo = self.Repo.create({'name': 'bla@example.com:foo-dev/bar', 'token':  '1'})
    addons_repo = self.Repo.create({'name': 'bla@example.com:ent-dev/bar', 'token': '1'})
    server_pr = self.Branch.create({
        'repo_id': server_repo.id,
        'name': 'refs/pull/123456'
    })
    mock_github.return_value = {
        'head' : {'label': 'foo-dev:foobar_branch'},
        'base' : {'ref': '10.0'},
        'state': 'open'
    }
    addons_pr = self.Branch.create({
        'repo_id': addons_repo.id,
        'name': 'refs/pull/789101'
    })
    addons_build = self.Build.create({
        'branch_id': addons_pr.id,
        'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
    })
    self.assertEqual((server_repo.id, 'master', 'default'), addons_build._get_closest_branch_name(server_repo.id))
