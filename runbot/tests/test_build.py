# -*- coding: utf-8 -*-
from unittest.mock import patch
from odoo.tools.config import configmanager
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
            'port': '1234',
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

    @patch('odoo.addons.runbot.models.build.fqdn')
    def test_guess_result(self, mock_fqdn):
        build = self.Build.create({
            'branch_id': self.branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            'port': '1234',
        })
        # Testing the guess_result computed field
        self.assertEqual(build.guess_result, '', 'A pending build guess_result should be empty')

        build.write({'state': 'done', 'result': 'ko'})
        build.invalidate_cache()
        self.assertEqual(build.guess_result, 'ko', 'A finished build should return the same as result')

        build.write({'state': 'testing'})
        build.invalidate_cache()
        self.assertEqual(build.guess_result, 'ok', 'A testing build without logs should be ok')

        self.env.cr.execute("""
            INSERT INTO ir_logging(name, type, path, func, line, build_id, level, message)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", ('testing', 'server', 'somewhere', 'test', 0, build.id, 'WARNING', 'blabla'))
        build.invalidate_cache()
        self.assertEqual(build.guess_result, 'warn', 'A testing build with warnings should be warn')

        self.env.cr.execute("""
            INSERT INTO ir_logging(name, type, path, func, line, build_id, level, message)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", ('testing', 'server', 'somewhere', 'test', 0, build.id, 'ERROR', 'blabla'))
        build.invalidate_cache()
        self.assertEqual(build.guess_result, 'ko', 'A testing build with errors should be ko')

    @patch('odoo.addons.runbot.models.build.os.mkdir')
    @patch('odoo.addons.runbot.models.build.grep')
    def test_build_cmd_log_db(self, mock_grep, mock_mkdir):
        """ test that the logdb connection URI is taken from the .odoorc file """
        uri = 'postgres://someone:pass@somewhere.com/db'
        self.env['ir.config_parameter'].sudo().set_param("runbot.runbot_logdb_uri", uri)
        build = self.Build.create({
            'branch_id': self.branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            'port': '1234',
        })
        cmd = build._cmd()[0]
        self.assertIn('--log-db=%s' % uri, cmd)

    def test_build_job_type_from_branch_default(self):
        """test build job_type is computed from branch default job_type"""
        build = self.Build.create({
            'branch_id': self.branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        self.assertEqual(build.job_type, 'all', "job_type should be the same as the branch")

    def test_build_job_type_from_branch_testing(self):
        """test build job_type is computed from branch"""
        self.branch.job_type = 'testing'
        build = self.Build.create({
            'branch_id': self.branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        self.assertEqual(build.job_type, 'testing', "job_type should be the same as the branch")

    def test_build_job_type_from_branch_none(self):
        """test build is not even created when branch job_type is none"""
        self.branch.job_type = 'none'
        build = self.Build.create({
            'branch_id': self.branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        self.assertEqual(build, self.Build, "build should be an empty recordset")

    def test_build_job_type_can_be_set(self):
        """test build job_type can be set to something different than the one on the branch"""
        self.branch.job_type = 'running'
        build = self.Build.create({
            'branch_id': self.branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            'job_type': 'testing'
        })
        self.assertEqual(build.job_type, 'testing', "job_type should be the one set on the build")

    def test_build_job_type_none(self):
        """test build job_type set to none does not create a build"""
        self.branch.job_type = 'running'
        build = self.Build.create({
            'branch_id': self.branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            'job_type': 'none'
        })
        self.assertEqual(build, self.Build, "build should be an empty recordset")

    @patch('odoo.addons.runbot.models.build._logger')
    def test_build_skip(self, mock_logger):
        """test build is skipped"""
        build = self.Build.create({
            'branch_id': self.branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            'port': '1234',
        })
        build._skip()
        self.assertEqual(build.state, 'done')
        self.assertEqual(build.result, 'skipped')

        other_build = self.Build.create({
            'branch_id': self.branch.id,
            'name': 'deadbeef0000ffffffffffffffffffffffffffff',
            'port': '1234',
        })
        other_build._skip(reason='A good reason')
        self.assertEqual(other_build.state, 'done')
        self.assertEqual(other_build.result, 'skipped')
        log_first_part = '%s skip %%s' % (other_build.dest)
        mock_logger.debug.assert_called_with(log_first_part, 'A good reason')

    def test_ask_kill_duplicate(self):
        """ Test that the _ask_kill method works on duplicate"""
        #mock_is_on_remote.return_value = True

        build1 = self.Build.create({
            'branch_id': self.branch_10.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        build2 = self.Build.create({
            'branch_id': self.branch_10.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        build2.write({'state': 'duplicate', 'duplicate_id': build1.id}) # this may not be usefull if we detect duplicate in same repo.

        self.assertEqual(build1.state, 'pending')
        build2._ask_kill()
        self.assertEqual(build1.state, 'done', 'A killed pending duplicate build should mark the real build as done')
        self.assertEqual(build1.result, 'skipped', 'A killed pending duplicate build should mark the real build as skipped')


def rev_parse(repo, branch_name):
    """
    simulate a rev parse by returning a fake hash of form
    'rp_odoo-dev/enterprise_saas-12.2__head'
    should be overwitten if a pr head should match a branch head
    """
    head_hash = 'rp_%s_%s_head' % (repo.name.split(':')[1], branch_name.split('/')[-1])
    return head_hash


class TestClosestBranch(common.TransactionCase):

    def branch_description(self, branch):
        branch_type = 'pull' if 'pull' in branch.name else 'branch'
        return '%s %s:%s' % (branch_type, branch.repo_id.name.split(':')[-1], branch.name.split('/')[-1])

    def assertClosest(self, branch, closest):
        extra_repo = branch.repo_id.dependency_ids[0]
        self.assertEqual(closest, branch._get_closest_branch(extra_repo.id), "build on %s didn't had the extected closest branch" % self.branch_description(branch))

    def assertDuplicate(self, branch1, branch2, b1_closest=None, b2_closest=None, noDuplicate=False):
        """
        Test that the creation of a build on branch1 and branch2 detects duplicate, no matter the order.
        Also test that build on branch1 closest_branch_name result is b1_closest if given
        Also test that build on branch2 closest_branch_name result is b2_closest if given
        """
        closest = {
            branch1: b1_closest,
            branch2: b2_closest,
        }
        for b1, b2 in [(branch1, branch2), (branch2, branch1)]:
            hash = '%s%s' % (b1.name, b2.name)
            build1 = self.Build.create({
                'branch_id': b1.id,
                'name': hash,
            })

            if b1_closest:
                self.assertClosest(b1, closest[b1])

            build2 = self.Build.create({
                'branch_id': b2.id,
                'name': hash,
            })

            if b2_closest:
                self.assertClosest(b2, closest[b2])
            if noDuplicate:
                self.assertNotEqual(build2.state, 'duplicate')
                self.assertFalse(build2.duplicate_id, "build on %s was detected as duplicate of build %s" % (self.branch_description(b2), build2.duplicate_id))
            else:
                self.assertEqual(build2.duplicate_id.id, build1.id, "build on %s wasn't detected as duplicate of build on %s" % (self.branch_description(b2), self.branch_description(b1)))
                self.assertEqual(build2.state, 'duplicate')

    def assertNoDuplicate(self, branch1, branch2, b1_closest=None, b2_closest=None):
        self.assertDuplicate(branch1, branch2, b1_closest=b1_closest, b2_closest=b2_closest, noDuplicate=True)

    def setUp(self):
        """ Setup repositories that mimick the Odoo repos """
        super(TestClosestBranch, self).setUp()
        self.Repo = self.env['runbot.repo']
        self.community_repo = self.Repo.create({'name': 'bla@example.com:odoo/odoo', 'token': '1'})
        self.enterprise_repo = self.Repo.create({'name': 'bla@example.com:odoo/enterprise', 'token': '1'})
        self.community_dev_repo = self.Repo.create({'name': 'bla@example.com:odoo-dev/odoo', 'token': '1'})
        self.enterprise_dev_repo = self.Repo.create({'name': 'bla@example.com:odoo-dev/enterprise', 'token': '1'})

        # tweak duplicates links between repos
        self.community_repo.duplicate_id = self.community_dev_repo.id
        self.community_dev_repo.duplicate_id = self.community_repo.id
        self.enterprise_repo.duplicate_id = self.enterprise_dev_repo.id
        self.enterprise_dev_repo.duplicate_id = self.enterprise_repo.id

        # create depenedencies to find Odoo server
        self.enterprise_repo.dependency_ids = self.community_repo
        self.enterprise_dev_repo.dependency_ids = self.community_dev_repo

        # Create some sticky branches
        self.Branch = self.env['runbot.branch']
        self.branch_odoo_master = self.Branch.create({
            'repo_id': self.community_repo.id,
            'name': 'refs/heads/master',
            'sticky': True,
        })
        self.branch_odoo_10 = self.Branch.create({
            'repo_id': self.community_repo.id,
            'name': 'refs/heads/10.0',
            'sticky': True,
        })
        self.branch_odoo_11 = self.Branch.create({
            'repo_id': self.community_repo.id,
            'name': 'refs/heads/11.0',
            'sticky': True,
        })

        self.branch_enterprise_master = self.Branch.create({
            'repo_id': self.enterprise_repo.id,
            'name': 'refs/heads/master',
            'sticky': True,
        })
        self.branch_enterprise_10 = self.Branch.create({
            'repo_id': self.enterprise_repo.id,
            'name': 'refs/heads/10.0',
            'sticky': True,
        })
        self.branch_enterprise_11 = self.Branch.create({
            'repo_id': self.enterprise_repo.id,
            'name': 'refs/heads/11.0',
            'sticky': True,
        })

        self.Build = self.env['runbot.build']

    @patch('odoo.addons.runbot.models.repo.runbot_repo._github')
    def test_pr_is_duplicate(self, mock_github):
        """ test PR is a duplicate of a dev branch build """

        mock_github.return_value = {
            'head': {'label': 'odoo-dev:10.0-fix-thing-moc'},
            'base': {'ref': '10.0'},
            'state': 'open'
        }

        dev_branch = self.Branch.create({
            'repo_id': self.community_dev_repo.id,
            'name': 'refs/heads/10.0-fix-thing-moc'
        })
        pr = self.Branch.create({
            'repo_id': self.community_repo.id,
            'name': 'refs/pull/12345'
        })
        self.assertDuplicate(dev_branch, pr)

    @patch('odoo.addons.runbot.models.branch.runbot_branch._is_on_remote')
    def test_closest_branch_01(self, mock_is_on_remote):
        """ test find a matching branch in a target repo based on branch name """
        mock_is_on_remote.return_value = True

        self.Branch.create({
            'repo_id': self.community_dev_repo.id,
            'name': 'refs/heads/10.0-fix-thing-moc'
        })
        addons_branch = self.Branch.create({
            'repo_id': self.enterprise_dev_repo.id,
            'name': 'refs/heads/10.0-fix-thing-moc'
        })

        self.assertEqual((addons_branch, 'exact'), addons_branch._get_closest_branch(self.enterprise_dev_repo.id))

    @patch('odoo.addons.runbot.models.repo.runbot_repo._github')
    def test_closest_branch_02(self, mock_github):

        """ test find two matching PR having the same head name """
        mock_github.return_value = {
            # "head label" is the repo:branch where the PR comes from
            # "base ref" is the target of the PR
            'head': {'label': 'odoo-dev:bar_branch'},
            'base': {'ref': 'saas-12.2'},
            'state': 'open'
        }

        # update to avoid test to break. we asume that bar_branch exists.
        # we may want to modify the branch creation to ensure that
        # -> first make all branches
        # -> then make all builds
        community_branch = self.Branch.create({
            'repo_id': self.community_dev_repo.id,
            'name': 'refs/heads/bar_branch'
        })

        # Create PR in community
        community_pr = self.Branch.create({
            'repo_id': self.community_repo.id,
            'name': 'refs/pull/123456'
        })
        enterprise_pr = self.Branch.create({
            'repo_id': self.enterprise_repo.id,
            'name': 'refs/pull/789101'
        })
        self.assertEqual((community_branch, 'exact PR'), enterprise_pr._get_closest_branch(self.community_repo.id))

    @patch('odoo.addons.runbot.models.repo.runbot_repo._github')
    @patch('odoo.addons.runbot.models.branch.runbot_branch._is_on_remote')
    def test_closest_branch_02_improved(self, mock_is_on_remote, mock_github):
        """ test that a PR in enterprise with a matching PR in Community
        uses the matching one"""

        mock_is_on_remote.return_value = True

        com_dev_branch = self.Branch.create({
            'repo_id': self.community_dev_repo.id,
            'name': 'refs/heads/saas-12.2-blabla'
        })

        ent_dev_branch = self.Branch.create({
            'repo_id': self.enterprise_dev_repo.id,
            'name': 'refs/heads/saas-12.2-blabla'
        })

        def github_side_effect(url, **kwargs):
            # "head label" is the repo:branch where the PR comes from
            # "base ref" is the target of the PR
            if url.endswith('/pulls/3721'):
                return {
                    'head': {'label': 'odoo-dev:saas-12.2-blabla'},
                    'base': {'ref': 'saas-12.2'},
                    'state': 'open'
                }
            elif url.endswith('/pulls/32156'):
                return {
                    'head': {'label': 'odoo-dev:saas-12.2-blabla'},
                    'base': {'ref': 'saas-12.2'},
                    'state': 'open'
                }
            else:
                self.assertTrue(False)

        mock_github.side_effect = github_side_effect

        ent_pr = self.Branch.create({
            'repo_id': self.enterprise_repo.id,
            'name': 'refs/pull/3721'
        })

        self.Branch.create({
            'repo_id': self.community_repo.id,
            'name': 'refs/pull/32156'
        })
        with patch('odoo.addons.runbot.models.repo.runbot_repo._git_rev_parse', new=rev_parse):
            self.assertDuplicate(
                ent_dev_branch,
                ent_pr,
                (com_dev_branch, 'exact'),
                (com_dev_branch, 'exact PR')
            )

    @patch('odoo.addons.runbot.models.branch.runbot_branch._is_on_remote')
    def test_closest_branch_03(self, mock_is_on_remote):
        """ test find a branch based on dashed prefix"""
        mock_is_on_remote.return_value = True
        addons_branch = self.Branch.create({
            'repo_id': self.enterprise_dev_repo.id,
            'name': 'refs/heads/10.0-fix-blah-blah-moc'
        })
        self.assertEqual((self.branch_odoo_10, 'prefix'), addons_branch._get_closest_branch(self.community_repo.id))

    @patch('odoo.addons.runbot.models.repo.runbot_repo._github')
    @patch('odoo.addons.runbot.models.branch.runbot_branch._is_on_remote')
    def test_closest_branch_03_05(self, mock_is_on_remote, mock_github):
        """ test that a PR in enterprise without a matching PR in Community
        and no branch in community"""
        mock_is_on_remote.return_value = True
        # comm_repo = self.repo
        # self.repo.write({'token': 1})

        ent_dev_branch = self.Branch.create({
            'repo_id': self.enterprise_dev_repo.id,
            'name': 'refs/heads/saas-12.2-blabla'
        })

        def github_side_effect(url, **kwargs):
            if url.endswith('/pulls/3721'):
                return {
                    'head': {'label': 'odoo-dev:saas-12.2-blabla'},
                    'base': {'ref': 'saas-12.2'},
                    'state': 'open'
                }
            elif url.endswith('/pulls/32156'):
                return {
                    'head': {'label': 'odoo-dev:saas-12.2-blabla'},
                    'base': {'ref': 'saas-12.2'},
                    'state': 'open'
                }
            else:
                self.assertTrue(False)

        mock_github.side_effect = github_side_effect

        com_branch = self.Branch.create({
            'repo_id': self.community_repo.id,
            'name': 'refs/heads/saas-12.2'
        })

        ent_pr = self.Branch.create({
            'repo_id': self.enterprise_repo.id,
            'name': 'refs/pull/3721'
        })
        with patch('odoo.addons.runbot.models.repo.runbot_repo._git_rev_parse', new=rev_parse):
            self.assertDuplicate(
                ent_pr,
                ent_dev_branch,
                (com_branch, 'pr_target'),
                (com_branch, 'prefix'),
            )

    @patch('odoo.addons.runbot.models.repo.runbot_repo._github')
    @patch('odoo.addons.runbot.models.branch.runbot_branch._is_on_remote')
    def test_closest_branch_04(self, mock_is_on_remote, mock_github):
        """ test that a PR in enterprise without a matching PR in Community
        uses the corresponding exact branch in community"""
        mock_is_on_remote.return_value = True

        com_dev_branch = self.Branch.create({
            'repo_id': self.community_dev_repo.id,
            'name': 'refs/heads/saas-12.2-blabla'
        })

        ent_dev_branch = self.Branch.create({
            'repo_id': self.enterprise_dev_repo.id,
            'name': 'refs/heads/saas-12.2-blabla'
        })

        def github_side_effect(*args, **kwargs):
            return {
                'head': {'label': 'odoo-dev:saas-12.2-blabla'},
                'base': {'ref': 'saas-12.2'},
                'state': 'open'
            }

        mock_github.side_effect = github_side_effect

        ent_pr = self.Branch.create({
            'repo_id': self.enterprise_repo.id,
            'name': 'refs/pull/3721'
        })
        with patch('odoo.addons.runbot.models.repo.runbot_repo._git_rev_parse', new=rev_parse):
            self.assertDuplicate(
                ent_dev_branch,
                ent_pr,
                (com_dev_branch, 'exact'),
                (com_dev_branch, 'no PR')
            )

    @patch('odoo.addons.runbot.models.repo.runbot_repo._github')
    def test_closest_branch_05(self, mock_github):
        """ test last resort value """
        mock_github.return_value = {
            'head': {'label': 'foo-dev:bar_branch'},
            'base': {'ref': '10.0'},
            'state': 'open'
        }

        server_pr = self.Branch.create({
            'repo_id': self.community_repo.id,
            'name': 'refs/pull/123456'
        })
        mock_github.return_value = {
            'head': {'label': 'foo-dev:foobar_branch'},
            'base': {'ref': '10.0'},
            'state': 'open'
        }
        addons_pr = self.Branch.create({
            'repo_id': self.enterprise_repo.id,
            'name': 'refs/pull/789101'
        })
        self.assertEqual((self.branch_odoo_10, 'pr_target'), addons_pr._get_closest_branch(self.community_repo.id))

    def test_closest_branch_05_master(self):
        """ test last resort value when nothing common can be found"""

        addons_branch = self.Branch.create({
            'repo_id': self.enterprise_dev_repo.id,
            'name': 'refs/head/badref-fix-foo'
        })
        self.assertEqual((self.branch_odoo_master, 'default'), addons_branch._get_closest_branch(self.community_repo.id))

    @patch('odoo.addons.runbot.models.branch.runbot_branch._is_on_remote')
    def test_no_duplicate_update(self, mock_is_on_remote):
        """push a dev branch in enterprise with same head as sticky, but with a matching branch in community"""
        mock_is_on_remote.return_value = True
        community_sticky_branch = self.Branch.create({
            'repo_id': self.community_repo.id,
            'name': 'refs/heads/saas-12.2',
            'sticky': True,
        })
        community_dev_branch = self.Branch.create({
            'repo_id': self.community_dev_repo.id,
            'name': 'refs/heads/saas-12.2-dev1',
        })
        enterprise_sticky_branch = self.Branch.create({
            'repo_id': self.enterprise_repo.id,
            'name': 'refs/heads/saas-12.2',
            'sticky': True,
        })
        enterprise_dev_branch = self.Branch.create({
            'repo_id': self.enterprise_dev_repo.id,
            'name': 'refs/heads/saas-12.2-dev1'
        })
        # we shouldn't have duplicate since community_dev_branch exists
        with patch('odoo.addons.runbot.models.repo.runbot_repo._git_rev_parse', new=rev_parse):
            # lets create an old enterprise build
            self.Build.create({
                'branch_id': enterprise_sticky_branch.id,
                'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            })
            self.assertNoDuplicate(
                enterprise_sticky_branch,
                enterprise_dev_branch,
                (community_sticky_branch, 'exact'),
                (community_dev_branch, 'exact'),
            )

    @patch('odoo.addons.runbot.models.repo.runbot_repo._github')
    def test_external_pr_closest_branch(self, mock_github):
        """ test last resort value target_name"""
        mock_github.return_value = {
            'head': {'label': 'external_repo:11.0-fix'},
            'base': {'ref': '11.0'},
            'state': 'open'
        }
        enterprise_pr = self.Branch.create({
            'repo_id': self.enterprise_repo.id,
            'name': 'refs/pull/123456'
        })
        dependency_repo = self.enterprise_repo.dependency_ids[0]
        closest_branch = enterprise_pr._get_closest_branch(dependency_repo.id)
        self.assertEqual(enterprise_pr._get_closest_branch(dependency_repo.id), (self.branch_odoo_11, 'pr_target'))

    @patch('odoo.addons.runbot.models.repo.runbot_repo._github')
    def test_external_pr_with_comunity_pr_closest_branch(self, mock_github):
        """ test matching external pr """
        mock_github.return_value = {
            'head': {'label': 'external_dev_repo:11.0-fix'},
            'base': {'ref': '11.0'},
            'state': 'open'
        }
        community_pr = self.Branch.create({
            'repo_id': self.community_repo.id,
            'name': 'refs/pull/123456'
        })
        mock_github.return_value = {
            'head': {'label': 'external_dev_repo:11.0-fix'}, # if repo doenst match, it wont work, maybe a fix to do here?
            'base': {'ref': '11.0'},
            'state': 'open'
        }
        enterprise_pr = self.Branch.create({
            'repo_id': self.enterprise_repo.id,
            'name': 'refs/pull/123'
        })
        with patch('odoo.addons.runbot.models.repo.runbot_repo._git_rev_parse', new=rev_parse):
            build = self.Build.create({
                'branch_id': enterprise_pr.id,
                'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            })
            dependency_repo = build.repo_id.dependency_ids[0]
            self.assertEqual(build.branch_id._get_closest_branch(dependency_repo.id), (community_pr, 'exact PR'))
            # this is working here because pull_head_name is set, but on runbot pull_head_name is empty for external pr. why?
