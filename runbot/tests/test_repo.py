# -*- coding: utf-8 -*-
import re
from unittest import skip
from unittest.mock import patch, Mock
from subprocess import CalledProcessError
from odoo.tests import common, TransactionCase
from odoo.tools import mute_logger
import logging
import time

from .common import RunbotCase, RunbotCaseMinimalSetup

_logger = logging.getLogger(__name__)


class TestRepo(RunbotCaseMinimalSetup):

    def setUp(self):
        super(TestRepo, self).setUp()
        self.commit_list = {}
        self.mock_root = self.patchers['repo_root_patcher']

    def test_base_fields(self):
        self.mock_root.return_value = '/tmp/static'

        repo = self.repo_server
        remote = self.remote_server
        # name = 'bla@example.com:base/server'
        self.assertEqual(repo.path, '/tmp/static/repo/server')
        self.assertEqual(remote.base_url, 'example.com/base/server')
        self.assertEqual(remote.short_name, 'base/server')
        self.assertEqual(remote.owner, 'base')
        self.assertEqual(remote.repo_name, 'server')

        # HTTPS
        remote.name = 'https://bla@example.com/base/server.git'
        self.assertEqual(remote.short_name, 'base/server')
        self.assertEqual(remote.owner, 'base')
        self.assertEqual(remote.repo_name, 'server')

        # LOCAL
        remote.name = '/path/somewhere/bar.git'
        self.assertEqual(remote.short_name, 'somewhere/bar')
        self.assertEqual(remote.owner, 'somewhere')
        self.assertEqual(remote.repo_name, 'bar')

    def test_repo_update_batches(self):
        """ Test that when finding new refs in a repo, the missing branches
        are created and new builds are created in pending state
        """
        self.repo_addons = self.repo_addons  # lazy repo_addons fails on union
        self.repo_server = self.repo_server  # lazy repo_addons fails on union
        self.additionnal_setup()
        self.start_patchers()
        max_bundle_id = self.env['runbot.bundle'].search([], order='id desc', limit=1).id or 0

        branch_name = 'master-test'

        def github(url, payload=None, ignore_errors=False, nb_tries=2, recursive=False):
            self.assertEqual(ignore_errors, False)
            self.assertEqual(url, '/repos/:owner/:repo/pulls/123')
            return {
                'base': {'ref': 'master'},
                'head': {'label': 'dev:%s' % branch_name, 'repo': {'full_name': 'dev/server'}},
                'title': '[IMP] Title',
                'body': 'Body',
                'user': {
                    'login': 'Pr author'
                },
            }

        repos = self.repo_addons | self.repo_server

        first_commit = [(
            'refs/%s/heads/%s' % (self.remote_server_dev.remote_name, branch_name),
            'd0d0caca',
            str(int(time.time())),
            'Marc Bidule',
            '<marc.bidule@somewhere.com>',
            'Server subject',
            'Marc Bidule',
            '<marc.bidule@somewhere.com>')]

        self.commit_list[self.repo_server.id] = first_commit

        self.patchers['github_patcher'].side_effect = github
        repos._update_batches()

        dev_branch = self.env['runbot.branch'].search([('remote_id', '=', self.remote_server_dev.id)])

        bundle = dev_branch.bundle_id
        self.assertEqual(dev_branch.name, branch_name, 'A new branch should have been created')

        batch = self.env['runbot.batch'].search([('bundle_id', '=', bundle.id)])
        self.assertEqual(len(batch), 1, 'Batch found')
        self.assertEqual(batch.commit_link_ids.commit_id.subject, 'Server subject')
        self.assertEqual(batch.state, 'preparing')
        self.assertEqual(dev_branch.head_name, 'd0d0caca')
        self.assertEqual(bundle.last_batch, batch)
        last_batch = batch

        # create a addons branch in the same bundle
        self.commit_list[self.repo_addons.id] = [('refs/%s/heads/%s' % (self.remote_addons_dev.remote_name, branch_name),
                                                  'deadbeef',
                                                   str(int(time.time())),
                                                  'Marc Bidule',
                                                  '<marc.bidule@somewhere.com>',
                                                  'Addons subject',
                                                  'Marc Bidule',
                                                  '<marc.bidule@somewhere.com>')]

        repos._update_batches()

        addons_dev_branch = self.env['runbot.branch'].search([('remote_id', '=', self.remote_addons_dev.id)])

        self.assertEqual(addons_dev_branch.bundle_id, bundle)

        self.assertEqual(dev_branch.head_name, 'd0d0caca', "Dev branch head name shoudn't have change")
        self.assertEqual(addons_dev_branch.head_name, 'deadbeef')

        branch_count = self.env['runbot.branch'].search_count([('remote_id', '=', self.remote_server_dev.id)])
        self.assertEqual(branch_count, 1, 'No new branch should have been created')

        batch = self.env['runbot.batch'].search([('bundle_id', '=', bundle.id)])
        self.assertEqual(last_batch, batch, "No new batch should have been created")
        self.assertEqual(bundle.last_batch, batch)
        self.assertEqual(batch.commit_link_ids.commit_id.mapped('subject'), ['Server subject', 'Addons subject'])

        # create a server pr in the same bundle with the same hash
        self.commit_list[self.repo_server.id] += [
            ('refs/%s/pull/123' % self.remote_server.remote_name,
             'd0d0caca',
             str(int(time.time())),
             'Marc Bidule',
             '<marc.bidule@somewhere.com>',
             'Another subject',
             'Marc Bidule',
             '<marc.bidule@somewhere.com>')]

        # Create Batches
        repos._update_batches()

        pull_request = self.env['runbot.branch'].search([('remote_id', '=', self.remote_server.id), ('name', '=', '123')])
        self.assertEqual(pull_request.bundle_id, bundle)

        self.assertEqual(dev_branch.head_name, 'd0d0caca')
        self.assertEqual(pull_request.head_name, 'd0d0caca')
        self.assertEqual(addons_dev_branch.head_name, 'deadbeef')

        self.assertEqual(dev_branch, self.env['runbot.branch'].search([('remote_id', '=', self.remote_server_dev.id)]))
        self.assertEqual(addons_dev_branch, self.env['runbot.branch'].search([('remote_id', '=', self.remote_addons_dev.id)]))

        batch = self.env['runbot.batch'].search([('bundle_id', '=', bundle.id)])
        self.assertEqual(last_batch, batch, "No new batch should have been created")
        self.assertEqual(bundle.last_batch, batch)
        self.assertEqual(batch.commit_link_ids.commit_id.mapped('subject'), ['Server subject', 'Addons subject'])

        # A new commit is found in the server repo
        self.commit_list[self.repo_server.id] = [
            (
                'refs/%s/heads/%s' % (self.remote_server_dev.remote_name, branch_name),
                'b00b',
                str(int(time.time())),
                'Marc Bidule',
                '<marc.bidule@somewhere.com>',
                'A new subject',
                'Marc Bidule',
                '<marc.bidule@somewhere.com>'
            ),
            (
                'refs/%s/pull/123' % self.remote_server.remote_name,
                'b00b',
                str(int(time.time())),
                'Marc Bidule',
                '<marc.bidule@somewhere.com>',
                'A new subject',
                'Marc Bidule',
                '<marc.bidule@somewhere.com>'
            )]

        # Create Batches
        repos._update_batches()

        self.assertEqual(dev_branch, self.env['runbot.branch'].search([('remote_id', '=', self.remote_server_dev.id)]))
        #self.assertEqual(pull_request + self.branch_server, self.env['runbot.branch'].search([('remote_id', '=', self.remote_server.id)]))
        self.assertEqual(addons_dev_branch, self.env['runbot.branch'].search([('remote_id', '=', self.remote_addons_dev.id)]))

        batch = self.env['runbot.batch'].search([('bundle_id', '=', bundle.id)])
        self.assertEqual(bundle.last_batch, batch)
        self.assertEqual(len(batch), 1, 'No new batch created, updated')
        self.assertEqual(batch.commit_link_ids.commit_id.mapped('subject'),  ['A new subject', 'Addons subject'], 'commits should have been updated')
        self.assertEqual(batch.state, 'preparing')

        self.assertEqual(dev_branch.head_name, 'b00b')
        self.assertEqual(pull_request.head_name, 'b00b')
        self.assertEqual(addons_dev_branch.head_name, 'deadbeef')

        # TODO move this
        # previous_build = self.env['runbot.build'].search([('repo_id', '=', repo.id), ('branch_id', '=', branch.id), ('name', '=', 'd0d0caca')])
        # self.assertEqual(previous_build.local_state, 'done', 'Previous pending build should be done')
        # self.assertEqual(previous_build.local_result, 'skipped', 'Previous pending build result should be skipped')

        batch.state = 'done'

        repos._update_batches()

        batch = self.env['runbot.batch'].search([('bundle_id', '=', bundle.id)])
        self.assertEqual(len(batch), 1, 'No new batch created, no head change')

        self.commit_list[self.repo_server.id] = [
            ('refs/%s/heads/%s' % (self.remote_server_dev.remote_name, branch_name),
             'dead1234',
             str(int(time.time())),
             'Marc Bidule',
             '<marc.bidule@somewhere.com>',
             'A last subject',
             'Marc Bidule',
             '<marc.bidule@somewhere.com>')]

        repos._update_batches()

        bundles = self.env['runbot.bundle'].search([('id', '>', max_bundle_id)])
        self.assertEqual(bundles, bundle)
        batches = self.env['runbot.batch'].search([('bundle_id', '=', bundle.id)])
        self.assertEqual(len(batches), 2, 'No preparing instance and new head -> new batch')
        self.assertEqual(bundle.last_batch.state, 'preparing')
        self.assertEqual(bundle.last_batch.commit_link_ids.commit_id.subject, 'A last subject')

        self.commit_list[self.repo_server.id] = first_commit  # branch reset hard to an old commit (and pr closed)

        repos._update_batches()

        batches = self.env['runbot.batch'].search([('bundle_id', '=', bundle.id)], order='id desc')
        last_batch = bundle.last_batch
        self.assertEqual(len(batches), 2, 'No new batch created, updated')
        self.assertEqual(last_batch.commit_link_ids.commit_id.mapped('subject'), ['Server subject'], 'commits should have been updated')
        self.assertEqual(last_batch.state, 'preparing')
        self.assertEqual(dev_branch.head_name, 'd0d0caca')

        def github2(url, payload=None, ignore_errors=False, nb_tries=2, recursive=False):
            self.assertEqual(ignore_errors, True)
            self.assertIn(url, ['/repos/:owner/:repo/statuses/d0d0caca', '/repos/:owner/:repo/statuses/deadbeef'])
            return {}

        self.patchers['github_patcher'].side_effect = github2
        last_batch._prepare()
        self.assertEqual(last_batch.commit_link_ids.commit_id.mapped('subject'), ['Server subject', 'Addons subject'])

        self.assertEqual(last_batch.state, 'ready')

        self.assertEqual(2, len(last_batch.slot_ids))
        self.assertEqual(2, len(last_batch.slot_ids.mapped('build_id')))

    @skip('This test is for performances. It needs a lot of real branches in DB to mean something')
    def test_repo_perf_find_new_commits(self):
        self.mock_root.return_value = '/tmp/static'
        repo = self.env['runbot.repo'].search([('name', '=', 'blabla')])

        self.commit_list[self.repo_server.id] = []

        # create 20000 branches and refs
        start_time = time.time()
        self.env['runbot.build'].search([], limit=5).write({'name': 'jflsdjflj'})

        for i in range(20005):
            self.commit_list[self.repo_server.id].append(['refs/heads/bidon-%05d' % i,
                                                          'd0d0caca %s' % i,
                                                          str(int(time.time())),
                                                          'Marc Bidule',
                                                          '<marc.bidule@somewhere.com>',
                                                          'A nice subject',
                                                          'Marc Bidule',
                                                          '<marc.bidule@somewhere.com>'])
        inserted_time = time.time()
        _logger.info('Insert took: %ssec', (inserted_time - start_time))
        repo._update_batches()

        _logger.info('Create pending builds took: %ssec', (time.time() - inserted_time))

    @common.warmup
    def test_times(self):
        def _test_times(model, setter, field_name):
            repo1 = self.repo_server
            repo2 = self.repo_addons

            with self.assertQueryCount(1):
                getattr(repo1, setter)(1.1)
            getattr(repo2, setter)(1.2)
            self.assertEqual(len(self.env[model].search([])), 2)
            self.assertEqual(repo1[field_name], 1.1)
            self.assertEqual(repo2[field_name], 1.2)

            getattr(repo1, setter)(1.3)
            getattr(repo2, setter)(1.4)

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

        _test_times('runbot.repo.hooktime', 'set_hook_time', 'hook_time')
        _test_times('runbot.repo.reftime', 'set_ref_time', 'get_ref_time')


class TestGithub(TransactionCase):

    def test_github(self):
        """ Test different github responses or failures"""

        project = self.env['runbot.project'].create({'name': 'Tests'})
        repo_server = self.env['runbot.repo'].create({
            'name': 'server',
            'project_id': project.id,
        })
        remote_server = self.env['runbot.remote'].create({
            'name': 'bla@example.com:base/server',
            'repo_id': repo_server.id,
        })

        #  self.assertEqual(remote_server._github('/repos/:owner/:repo/statuses/abcdef', dict(), ignore_errors=True), None, 'A repo without token should return None')
        remote_server.token = 'abc'

        import requests
        with patch('odoo.addons.runbot.models.repo.requests.Session') as mock_session, patch('time.sleep') as mock_sleep:
            mock_sleep.return_value = None
            with self.assertRaises(Exception, msg='should raise an exception with ignore_errors=False'):
                mock_session.return_value.post.side_effect = requests.HTTPError('301: Bad gateway')
                remote_server._github('/repos/:owner/:repo/statuses/abcdef', {'foo': 'bar'}, ignore_errors=False)

            mock_session.return_value.post.reset_mock()
            with self.assertLogs(logger='odoo.addons.runbot.models.repo') as assert_log:
                remote_server._github('/repos/:owner/:repo/statuses/abcdef', {'foo': 'bar'}, ignore_errors=True)
                self.assertIn('Ignored github error', assert_log.output[0])

            self.assertEqual(2, mock_session.return_value.post.call_count, "_github method should try two times by default")

            mock_session.return_value.post.reset_mock()
            mock_session.return_value.post.side_effect = [requests.HTTPError('301: Bad gateway'), Mock()]
            with self.assertLogs(logger='odoo.addons.runbot.models.repo') as assert_log:
                remote_server._github('/repos/:owner/:repo/statuses/abcdef', {'foo': 'bar'}, ignore_errors=True)
                self.assertIn('Success after 2 tries', assert_log.output[0])

            self.assertEqual(2, mock_session.return_value.post.call_count, "_github method should try two times by default")


class TestFetch(RunbotCase):

    def setUp(self):
        super(TestFetch, self).setUp()
        self.mock_root = self.patchers['repo_root_patcher']
        self.fetch_count = 0
        self.force_failure = False

    def mock_git_helper(self):
        """Helper that returns a mock for repo._git()"""
        def mock_git(repo, cmd):
            self.assertIn('fetch', cmd)
            self.fetch_count += 1
            if self.fetch_count < 3 or self.force_failure:
                raise CalledProcessError(128, cmd, 'Dummy Error'.encode('utf-8'))
            else:
                return True
        return mock_git

    @patch('time.sleep', return_value=None)
    def test_update_fetch_cmd(self, mock_time):
        """ Test that git fetch is tried multiple times before disabling host """

        host = self.env['runbot.host']._get_current()

        self.assertFalse(host.assigned_only)
        # Ensure that Host is not disabled if fetch succeeds after 3 tries
        with mute_logger("odoo.addons.runbot.models.repo"):
            self.repo_server._update_fetch_cmd()

        self.assertFalse(host.assigned_only, "Host should not be disabled when fetch succeeds")
        self.assertEqual(self.fetch_count, 3)

        self.force_failure = True

        with mute_logger("odoo.addons.runbot.models.repo"):
            self.repo_server._update_fetch_cmd()
        self.assertFalse(host.assigned_only, "Host should not be disabled when fetch fails by default")

        self.fetch_count = 0
        self.env['ir.config_parameter'].sudo().set_param('runbot.runbot_disable_host_on_fetch_failure', True)
        with mute_logger("odoo.addons.runbot.models.repo"):
            self.repo_server._update_fetch_cmd()
        self.assertTrue(host.assigned_only, "Host should be disabled when fetch fails and runbot_disable_host_on_fetch_failure is set")
        self.assertEqual(self.fetch_count, 5)


class TestIdentityFile(RunbotCase):

        def check_output_helper(self):
            """Helper that returns a mock for repo._git()"""
            def mock_check_output(cmd, *args, **kwargs):
                expected_option = '-c core.sshCommand=ssh -i \/.+\/\.ssh\/fake_identity'
                git_cmd = ' '.join(cmd)
                self.assertTrue(re.search(expected_option, git_cmd), '%s did not match %s' % (git_cmd, expected_option))
                return Mock()

            return mock_check_output

        def test_identity_file(self):
            """test that the identity file is used in git command"""

            self.stop_patcher('git_patcher')
            self.start_patcher('check_output_patcher', 'odoo.addons.runbot.models.repo.subprocess.check_output', new=self.check_output_helper())

            self.repo_server.identity_file = 'fake_identity'

            with mute_logger("odoo.addons.runbot.models.repo"):
                self.repo_server._update_fetch_cmd()


class TestRepoScheduler(RunbotCase):

    def setUp(self):
        # as the _scheduler method commits, we need to protect the database
        super(TestRepoScheduler, self).setUp()

        mock_root = self.patchers['repo_root_patcher']
        mock_root.return_value = '/tmp/static'

    @patch('odoo.addons.runbot.models.build.BuildResult._kill')
    @patch('odoo.addons.runbot.models.build.BuildResult._schedule')
    @patch('odoo.addons.runbot.models.build.BuildResult._init_pendings')
    def test_repo_scheduler(self, mock_init_pendings, mock_schedule, mock_kill):

        self.env['ir.config_parameter'].set_param('runbot.runbot_workers', 6)
        builds = []
        # create 6 builds that are testing on the host to verify that
        # workers are not overfilled
        for _ in range(6):
            build = self.Build.create({
                'params_id': self.base_params.id,
                'build_type': 'normal',
                'local_state': 'testing',
                'host': 'host.runbot.com'
            })
            builds.append(build)
        # now the pending build that should stay unasigned
        scheduled_build = self.Build.create({
            'params_id': self.base_params.id,
            'build_type': 'scheduled',
            'local_state': 'pending',
        })
        builds.append(scheduled_build)
        # create the build that should be assigned once a slot is available
        build = self.Build.create({
            'params_id': self.base_params.id,
            'build_type': 'normal',
            'local_state': 'pending',
        })
        builds.append(build)
        host = self.env['runbot.host']._get_current()
        self.Runbot._scheduler(host)

        build.invalidate_cache()
        scheduled_build.invalidate_cache()
        self.assertFalse(build.host)
        self.assertFalse(scheduled_build.host)

        # give some room for the pending build
        builds[0].write({'local_state': 'done'})

        self.Runbot._scheduler(host)


class TestGetRefs(RunbotCase):
    def setUp(self):
        super().setUp()
        self.test_refs = []

    def mock_git_helper(self):
        """Helper that returns a mock for repo._git()"""
        def mock_git(repo, cmd):
            self.assertIn('for-each-ref', cmd)
            self.assertIn('refs/*/pull/*', cmd)
            return '\n'.join(['\x00'.join(ref_data) for ref_data in self.test_refs])
        return mock_git

    def test_get_refs(self):
        current = time.time()
        commit_time = str(int(current) - 5000)
        self.remote_server_dev.fetch_pull = True
        to_ignore = {'242': current - 100}
        good_ref = (
            'refs/bla-dev/heads/master-test-branch-rbt',
            'da39a3ee5e6b4b0d3255bfef95601890afd80709',
            commit_time,
            'foobarman',
            '<foobarman@somewhere.com>',
            '[IMP] mail: better tests',
            'foobarman',
            '<foobarman@somewhere.com>',
        )
        bad_ref = (
            'refs/bla-dev/heads/1703',
            'e9b396d2dddffdb373bf2c6ad073696aa25b4f68',
            commit_time,
            'foobarman',
            '<foobarman@somewhere.com>',
            '[FIX] foo: bar',
            'foobarman',
            '<foobarman@somewhere.com>',
        )
        to_ignore_ref = (
            'refs/bla-dev/pull/242',
            'ee89a48b76b58f4b3b0a7ee2c558dd8d936f6b12',
            commit_time,
            'foobarman',
            '<foobarman@somewhere.com>',
            '[IMP] blah: blah',
            'foobarman',
            '<foobarman@somewhere.com>',
        )
        self.test_refs.extend([good_ref, bad_ref, to_ignore_ref])
        refs = self.repo_server._get_refs(ignore=to_ignore)
        self.assertIn(good_ref, refs, 'A valid branch should appear in refs')
        self.assertNotIn(bad_ref, refs, 'A branch name that is an integer should be filtered out')
        self.assertNotIn(to_ignore_ref, refs, 'An explicitely ignored branch should be filtered out')
