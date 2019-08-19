# -*- coding: utf-8 -*-
import datetime
from unittest import skip
from unittest.mock import patch, Mock
from odoo.tests import common
import logging
import odoo
import time

import datetime

_logger = logging.getLogger(__name__)


class Test_Repo(common.TransactionCase):

    def setUp(self):
        super(Test_Repo, self).setUp()
        self.Repo = self.env['runbot.repo']
        self.commit_list = []

    def mock_git_helper(self):
        """Helper that returns a mock for repo._git()"""
        def mock_git(repo, cmd):
            if cmd[0] == 'for-each-ref' and self.commit_list:
                return '\n'.join(['\0'.join(commit_fields) for commit_fields in self.commit_list])
        return mock_git

    @patch('odoo.addons.runbot.models.repo.runbot_repo._root')
    def test_base_fields(self, mock_root):
        mock_root.return_value = '/tmp/static'
        repo = self.Repo.create({'name': 'bla@example.com:foo/bar'})
        self.assertEqual(repo.path, '/tmp/static/repo/bla_example.com_foo_bar')

        self.assertEqual(repo.base, 'example.com/foo/bar')
        self.assertEqual(repo.short_name, 'foo/bar')

        https_repo = self.Repo.create({'name': 'https://bla@example.com/user/rep.git'})
        self.assertEqual(https_repo.short_name, 'user/rep')

        local_repo = self.Repo.create({'name': '/path/somewhere/rep.git'})
        self.assertEqual(local_repo.short_name, 'somewhere/rep')

    @patch('odoo.addons.runbot.models.repo.runbot_repo._root')
    @patch('odoo.addons.runbot.models.repo.runbot_repo._get_fetch_head_time')
    def test_repo_create_pending_builds(self, mock_fetch_head_time, mock_root):
        """ Test that when finding new refs in a repo, the missing branches
        are created and new builds are created in pending state
        """
        mock_root.return_value = '/tmp/static'
        repo = self.Repo.create({'name': 'bla@example.com:foo/bar'})

        # create another repo and branch to ensure there is no mismatch
        other_repo = self.Repo.create({'name': 'bla@example.com:foo/foo'})
        self.env['runbot.branch'].create({
            'repo_id': other_repo.id,
            'name': 'refs/heads/bidon'
        })

        self.commit_list = [('refs/heads/bidon',
                             'd0d0caca',
                             datetime.datetime.now().strftime("%Y-%m-%d, %H:%M:%S"),
                             'Marc Bidule',
                             '<marc.bidule@somewhere.com>',
                             'A nice subject',
                             'Marc Bidule',
                             '<marc.bidule@somewhere.com>')]
        mock_fetch_head_time.side_effect = [100000.0, 100001.0, 100002.0]

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
        self.assertEqual(build.subject, 'Another subject')
        self.assertEqual(build.local_state, 'pending')
        self.assertFalse(build.local_result)

        previous_build = self.env['runbot.build'].search([('repo_id', '=', repo.id), ('branch_id', '=', branch.id), ('name', '=', 'd0d0caca')])
        self.assertEqual(previous_build.local_state, 'done', 'Previous pending build should be done')
        self.assertEqual(previous_build.local_result, 'skipped', 'Previous pending build result should be skipped')

    @skip('This test is for performances. It needs a lot of real branches in DB to mean something')
    @patch('odoo.addons.runbot.models.repo.runbot_repo._root')
    def test_repo_perf_find_new_commits(self, mock_root):
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

    def test_github(self):
        """ Test different github responses or failures"""
        repo = self.Repo.create({'name': 'bla@example.com:foo/foo'})
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

class Test_Repo_Scheduler(common.TransactionCase):

    @patch('odoo.addons.runbot.models.repo.runbot_repo._root')
    def setUp(self, mock_root):
        # as the _scheduler method commits, we need to protect the database
        registry = odoo.registry()
        registry.enter_test_mode()
        self.addCleanup(registry.leave_test_mode)
        super(Test_Repo_Scheduler, self).setUp()

        mock_root.return_value = '/tmp/static'
        self.Repo_model = self.env['runbot.repo']
        self.Branch_model = self.env['runbot.branch']
        self.foo_repo = self.Repo_model.create({'name': 'bla@example.com:foo/bar'})

        self.foo_branch = self.Branch_model.create({
            'repo_id': self.foo_repo.id,
            'name': 'refs/head/foo'
        })

    @patch('odoo.addons.runbot.models.build.runbot_build._reap')
    @patch('odoo.addons.runbot.models.build.runbot_build._kill')
    @patch('odoo.addons.runbot.models.build.runbot_build._schedule')
    @patch('odoo.addons.runbot.models.host.fqdn')
    def test_repo_scheduler(self, mock_fqdn, mock_schedule, mock_kill, mock_reap):
        mock_fqdn.return_value = 'test_host'
        self.env['ir.config_parameter'].set_param('runbot.runbot_workers', 6)
        Build_model = self.env['runbot.build']
        builds = []
        # create 6 builds that are testing on the host to verify that
        # workers are not overfilled
        for build_name in ['a', 'b', 'c', 'd', 'e', 'f']:
            build = Build_model.create({
                'branch_id': self.foo_branch.id,
                'name': build_name,
                'port': '1234',
                'build_type': 'normal',
                'local_state': 'testing',
                'host': 'test_host'
            })
            builds.append(build)
        # now the pending build that should stay unasigned
        scheduled_build = Build_model.create({
            'branch_id': self.foo_branch.id,
            'name': 'sched_build',
            'port': '1234',
            'build_type': 'scheduled',
            'local_state': 'pending',
        })
        builds.append(scheduled_build)
        # create the build that should be assigned once a slot is available
        build = Build_model.create({
            'branch_id': self.foo_branch.id,
            'name': 'foobuild',
            'port': '1234',
            'build_type': 'normal',
            'local_state': 'pending',
        })
        builds.append(build)

        self.foo_repo._scheduler()

        build.invalidate_cache()
        scheduled_build.invalidate_cache()
        self.assertFalse(build.host)
        self.assertFalse(scheduled_build.host)

        # give some room for the pending build
        Build_model.search([('name', '=', 'a')]).write({'local_state': 'done'})

        self.foo_repo._scheduler()
        build.invalidate_cache()
        scheduled_build.invalidate_cache()
        self.assertEqual(build.host, 'test_host')
        self.assertFalse(scheduled_build.host)
