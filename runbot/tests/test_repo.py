# -*- coding: utf-8 -*-
import datetime
from unittest import skip
from unittest.mock import patch, Mock
from odoo.tests import common, TransactionCase
import logging
import odoo
import time

from .common import RunbotCase

_logger = logging.getLogger(__name__)


class Test_Repo(RunbotCase):

    def setUp(self):
        super(Test_Repo, self).setUp()
        self.commit_list = []
        self.mock_root = self.patchers['repo_root_patcher']

    def mock_git_helper(self):
        """Helper that returns a mock for repo._git()"""
        def mock_git(repo, cmd):
            if cmd[0] == 'for-each-ref' and self.commit_list:
                return '\n'.join(['\0'.join(commit_fields) for commit_fields in self.commit_list])
        return mock_git

    def test_base_fields(self):
        self.mock_root.return_value = '/tmp/static'
        repo = self.Repo.create({'name': 'bla@example.com:foo/bar'})
        self.assertEqual(repo.path, '/tmp/static/repo/bla_example.com_foo_bar')

        self.assertEqual(repo.base, 'example.com/foo/bar')
        self.assertEqual(repo.short_name, 'foo/bar')

        https_repo = self.Repo.create({'name': 'https://bla@example.com/user/rep.git'})
        self.assertEqual(https_repo.short_name, 'user/rep')

        local_repo = self.Repo.create({'name': '/path/somewhere/rep.git'})
        self.assertEqual(local_repo.short_name, 'somewhere/rep')

    @patch('odoo.addons.runbot.models.repo.runbot_repo._get_fetch_head_time')
    def test_repo_create_pending_builds(self, mock_fetch_head_time):
        """ Test that when finding new refs in a repo, the missing branches
        are created and new builds are created in pending state
        """
        self.mock_root.return_value = '/tmp/static'
        repo = self.Repo.create({'name': 'bla@example.com:foo/bar'})

        # create another repo and branch to ensure there is no mismatch
        other_repo = self.Repo.create({'name': 'bla@example.com:foo/foo'})
        self.env['runbot.branch'].create({
            'repo_id': other_repo.id,
            'name': 'refs/heads/bidon'
        })

        first_commit = [('refs/heads/bidon',
                             'd0d0caca',
                             datetime.datetime.now().strftime("%Y-%m-%d, %H:%M:%S"),
                             'Marc Bidule',
                             '<marc.bidule@somewhere.com>',
                             'A nice subject',
                             'Marc Bidule',
                             '<marc.bidule@somewhere.com>')]
        self.commit_list = first_commit

        def counter():
            i = 100000
            while True:
                i += 1
                yield i

        mock_fetch_head_time.side_effect = counter()

        with patch('odoo.addons.runbot.models.repo.runbot_repo._git', new=self.mock_git_helper()):
            repo._create_pending_builds()

        branch = self.env['runbot.branch'].search([('repo_id', '=', repo.id)])
        self.assertEqual(branch.name, 'refs/heads/bidon', 'A new branch should have been created')

        build = self.env['runbot.build'].search([('repo_id', '=', repo.id), ('branch_id', '=', branch.id)])
        self.assertEqual(len(build), 1, 'Build found')
        self.assertEqual(build.subject, 'A nice subject')
        self.assertEqual(build.local_state, 'pending')
        self.assertFalse(build.local_result)

        # Simulate that a new commit is found in the other repo
        self.commit_list = [('refs/heads/bidon',
                             'deadbeef',
                             datetime.datetime.now().strftime("%Y-%m-%d, %H:%M:%S"),
                             'Marc Bidule',
                             '<marc.bidule@somewhere.com>',
                             'A better subject',
                             'Marc Bidule',
                             '<marc.bidule@somewhere.com>')]

        with patch('odoo.addons.runbot.models.repo.runbot_repo._git', new=self.mock_git_helper()):
            other_repo._create_pending_builds()

        branch_count = self.env['runbot.branch'].search_count([('repo_id', '=', repo.id)])
        self.assertEqual(branch_count, 1, 'No new branch should have been created')

        build = self.env['runbot.build'].search([('repo_id', '=', repo.id), ('branch_id', '=', branch.id)])
        self.assertEqual(build.subject, 'A nice subject')
        self.assertEqual(build.local_state, 'pending')
        self.assertFalse(build.local_result)

        # A new commit is found in the first repo, the previous pending build should be skipped
        self.commit_list = [('refs/heads/bidon',
                             'b00b',
                             datetime.datetime.now().strftime("%Y-%m-%d, %H:%M:%S"),
                             'Marc Bidule',
                             '<marc.bidule@somewhere.com>',
                             'Another subject',
                             'Marc Bidule',
                             '<marc.bidule@somewhere.com>')]

        with patch('odoo.addons.runbot.models.repo.runbot_repo._git', new=self.mock_git_helper()):
            repo._create_pending_builds()

        branch_count = self.env['runbot.branch'].search_count([('repo_id', '=', repo.id)])
        self.assertEqual(branch_count, 1, 'No new branch should have been created')

        build = self.env['runbot.build'].search([('repo_id', '=', repo.id), ('branch_id', '=', branch.id), ('name', '=', 'b00b')])
        self.assertEqual(len(build), 1)
        self.assertEqual(build.subject, 'Another subject')
        self.assertEqual(build.local_state, 'pending')
        self.assertFalse(build.local_result)

        previous_build = self.env['runbot.build'].search([('repo_id', '=', repo.id), ('branch_id', '=', branch.id), ('name', '=', 'd0d0caca')])
        self.assertEqual(previous_build.local_state, 'done', 'Previous pending build should be done')
        self.assertEqual(previous_build.local_result, 'skipped', 'Previous pending build result should be skipped')

        self.commit_list = first_commit  # branch reseted hard to an old commit
        builds = self.env['runbot.build'].search([('repo_id', '=', repo.id), ('branch_id', '=', branch.id), ('name', '=', 'd0d0caca')])
        self.assertEqual(len(builds), 1)
        with patch('odoo.addons.runbot.models.repo.runbot_repo._git', new=self.mock_git_helper()):
            repo._create_pending_builds()

        last_build = self.env['runbot.build'].search([], limit=1)
        self.assertEqual(last_build.name, 'd0d0caca')
        builds = self.env['runbot.build'].search([('repo_id', '=', repo.id), ('branch_id', '=', branch.id), ('name', '=', 'd0d0caca')])
        self.assertEqual(len(builds), 2)
        # self.assertEqual(last_build.duplicate_id, previous_build) False because previous_build is skipped
        with patch('odoo.addons.runbot.models.repo.runbot_repo._git', new=self.mock_git_helper()):
            other_repo._create_pending_builds()
        builds = self.env['runbot.build'].search([('repo_id', '=', repo.id), ('branch_id', '=', branch.id), ('name', '=', 'd0d0caca')])
        self.assertEqual(len(builds), 2)


    @skip('This test is for performances. It needs a lot of real branches in DB to mean something')
    def test_repo_perf_find_new_commits(self):
        mock_root.return_value = '/tmp/static'
        repo = self.env['runbot.repo'].search([('name', '=', 'blabla')])

        self.commit_list = []

        # create 20000 branches and refs
        start_time = time.time()
        self.env['runbot.build'].search([], limit=5).write({'name': 'jflsdjflj'})

        for i in range(20005):
            self.commit_list.append(['refs/heads/bidon-%05d' % i,
                                     'd0d0caca %s' % i,
                                     datetime.datetime.now().strftime("%Y-%m-%d, %H:%M:%S"),
                                     'Marc Bidule',
                                     '<marc.bidule@somewhere.com>',
                                     'A nice subject',
                                     'Marc Bidule',
                                     '<marc.bidule@somewhere.com>'])
        inserted_time = time.time()
        _logger.info('Insert took: %ssec', (inserted_time - start_time))
        with patch('odoo.addons.runbot.models.repo.runbot_repo._git', new=self.mock_git_helper()):
            repo._create_pending_builds()

        _logger.info('Create pending builds took: %ssec', (time.time() - inserted_time))

    def test_times(self):
        def _test_times(model, field_name):
            repo1 = self.Repo.create({'name': 'bla@example.com:foo/bar'})
            repo2 = self.Repo.create({'name': 'bla@example.com:foo2/bar2'})
            count = self.cr.sql_log_count
            repo1[field_name] = 1.1
            self.assertEqual(self.cr.sql_log_count - count, 1, "Only one insert should have been triggered")
            repo2[field_name] = 1.2
            self.assertEqual(len(self.env[model].search([])), 2)
            self.assertEqual(repo1[field_name], 1.1)
            self.assertEqual(repo2[field_name], 1.2)

            repo1[field_name] = 1.3
            repo2[field_name] = 1.4

            self.assertEqual(len(self.env[model].search([])), 4)
            self.assertEqual(repo1[field_name], 1.3)
            self.assertEqual(repo2[field_name], 1.4)

            self.Repo.invalidate_cache()
            self.assertEqual(repo1[field_name], 1.3)
            self.assertEqual(repo2[field_name], 1.4)

            self.Repo._gc_times()

            self.assertEqual(len(self.env[model].search([])), 2)
            self.assertEqual(repo1[field_name], 1.3)
            self.assertEqual(repo2[field_name], 1.4)

        _test_times('runbot.repo.hooktime', 'hook_time')
        _test_times('runbot.repo.reftime', 'get_ref_time')



class Test_Github(TransactionCase):
    def test_github(self):
        """ Test different github responses or failures"""
        repo = self.env['runbot.repo'].create({'name': 'bla@example.com:foo/foo'})
        self.assertEqual(repo._github('/repos/:owner/:repo/statuses/abcdef', dict(), ignore_errors=True), None, 'A repo without token should return None')
        repo.token = 'abc'
        with patch('odoo.addons.runbot.models.repo.requests.Session') as mock_session:
            with self.assertRaises(Exception, msg='should raise an exception with ignore_errors=False'):
                mock_session.return_value.post.side_effect = Exception('301: Bad gateway')
                repo._github('/repos/:owner/:repo/statuses/abcdef', {'foo': 'bar'}, ignore_errors=False)

            mock_session.return_value.post.reset_mock()
            with self.assertLogs(logger='odoo.addons.runbot.models.repo') as assert_log:
                repo._github('/repos/:owner/:repo/statuses/abcdef', {'foo': 'bar'}, ignore_errors=True)
                self.assertIn('Ignored github error', assert_log.output[0])

            self.assertEqual(2, mock_session.return_value.post.call_count, "_github method should try two times by default")

            mock_session.return_value.post.reset_mock()
            mock_session.return_value.post.side_effect = [Exception('301: Bad gateway'), Mock()]
            with self.assertLogs(logger='odoo.addons.runbot.models.repo') as assert_log:
                repo._github('/repos/:owner/:repo/statuses/abcdef', {'foo': 'bar'}, ignore_errors=True)
                self.assertIn('Success after 2 tries', assert_log.output[0])

            self.assertEqual(2, mock_session.return_value.post.call_count, "_github method should try two times by default")


class Test_Repo_Scheduler(RunbotCase):

    def setUp(self  ):
        # as the _scheduler method commits, we need to protect the database
        registry = odoo.registry()
        registry.enter_test_mode()
        self.addCleanup(registry.leave_test_mode)
        super(Test_Repo_Scheduler, self).setUp()

        self.fqdn_patcher = patch('odoo.addons.runbot.models.host.fqdn')
        mock_root = self.patchers['repo_root_patcher']
        mock_root.return_value = '/tmp/static'

        self.foo_repo = self.Repo.create({'name': 'bla@example.com:foo/bar'})

        self.foo_branch = self.Branch.create({
            'repo_id': self.foo_repo.id,
            'name': 'refs/head/foo'
        })

    @patch('odoo.addons.runbot.models.build.runbot_build._kill')
    @patch('odoo.addons.runbot.models.build.runbot_build._schedule')
    @patch('odoo.addons.runbot.models.build.runbot_build._init_pendings')
    def test_repo_scheduler(self, mock_init_pendings, mock_schedule, mock_kill):
        self.env['ir.config_parameter'].set_param('runbot.runbot_workers', 6)
        builds = []
        # create 6 builds that are testing on the host to verify that
        # workers are not overfilled
        for build_name in ['a', 'b', 'c', 'd', 'e', 'f']:
            build = self.create_build({
                'branch_id': self.foo_branch.id,
                'name': build_name,
                'port': '1234',
                'build_type': 'normal',
                'local_state': 'testing',
                'host': 'host.runbot.com'
            })
            builds.append(build)
        # now the pending build that should stay unasigned
        scheduled_build = self.create_build({
            'branch_id': self.foo_branch.id,
            'name': 'sched_build',
            'port': '1234',
            'build_type': 'scheduled',
            'local_state': 'pending',
        })
        builds.append(scheduled_build)
        # create the build that should be assigned once a slot is available
        build = self.create_build({
            'branch_id': self.foo_branch.id,
            'name': 'foobuild',
            'port': '1234',
            'build_type': 'normal',
            'local_state': 'pending',
        })
        builds.append(build)
        host = self.env['runbot.host']._get_current()
        self.foo_repo._scheduler(host)

        build.invalidate_cache()
        scheduled_build.invalidate_cache()
        self.assertFalse(build.host)
        self.assertFalse(scheduled_build.host)

        # give some room for the pending build
        self.Build.search([('name', '=', 'a')]).write({'local_state': 'done'})

        self.foo_repo._scheduler(host)
        build.invalidate_cache()
        scheduled_build.invalidate_cache()
        self.assertEqual(build.host, 'host.runbot.com')
        self.assertFalse(scheduled_build.host)
