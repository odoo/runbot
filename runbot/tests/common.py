# -*- coding: utf-8 -*-
import datetime
import time
from odoo.tests.common import TransactionCase
from unittest.mock import patch, DEFAULT

import logging

_logger = logging.getLogger(__name__)


class RunbotCase(TransactionCase):

    def mock_git_helper(self):
        """Helper that returns a mock for repo._git()"""
        def mock_git(repo, cmd):
            if cmd[:2] == ['show', '-s'] or cmd[:3] == ['show', '--pretty="%H -- %s"', '-s']:
                return 'commit message for %s' % cmd[-1]
            if cmd[:2] == ['cat-file', '-e']:
                return True
            if cmd[0] == 'for-each-ref':
                if self.commit_list.get(repo.id):
                    return '\n'.join(['\0'.join(commit_fields) for commit_fields in self.commit_list[repo.id]])
                else:
                    return ''
            if cmd[0] == 'diff':
                return self.diff
            else:
                _logger.warning('Unsupported mock command %s' % cmd)
        return mock_git

    def push_commit(self, remote, branch_name, subject, sha=None, tstamp=None, committer=None, author=None):
        """Helper to simulate a commit pushed"""

        committer = committer or "Marc Bidule"
        commiter_email = '%s@somewhere.com' % committer.lower().replace(' ', '_')
        author = author or committer
        author_email = '%s@somewhere.com' % author.lower().replace(' ', '_')
        self.commit_list[self.repo_server.id] = [(
            'refs/%s/heads/%s' % (remote.remote_name, branch_name),
            sha or 'd0d0caca',
            str(tstamp or int(time.time())),
            committer,
            commiter_email,
            subject,
            author,
            author_email)]

    def setUp(self):
        super().setUp()
        self.Project = self.env['runbot.project']
        self.Build = self.env['runbot.build']
        self.BuildParameters = self.env['runbot.build.params']
        self.Repo = self.env['runbot.repo'].with_context(mail_create_nolog=True, mail_notrack=True)
        self.Remote = self.env['runbot.remote'].with_context(mail_create_nolog=True, mail_notrack=True)
        self.Trigger = self.env['runbot.trigger'].with_context(mail_create_nolog=True, mail_notrack=True)
        self.Branch = self.env['runbot.branch']
        self.Bundle = self.env['runbot.bundle']
        self.Batch = self.env['runbot.batch']
        self.Version = self.env['runbot.version']
        self.Config = self.env['runbot.build.config'].with_context(mail_create_nolog=True, mail_notrack=True)
        self.Step = self.env['runbot.build.config.step'].with_context(mail_create_nolog=True, mail_notrack=True)
        self.Commit = self.env['runbot.commit']
        self.Runbot = self.env['runbot.runbot']
        self.project = self.env['runbot.project'].create({'name': 'Tests'})
        self.repo_server = self.Repo.create({
            'name': 'server',
            'project_id': self.project.id,
            'server_files': 'server.py',
            'addons_paths': 'addons,core/addons'
        })
        self.repo_addons = self.Repo.create({
            'name': 'addons',
            'project_id': self.project.id,
        })

        self.remote_server = self.Remote.create({
            'name': 'bla@example.com:base/server',
            'repo_id': self.repo_server.id,
            'token': '123',
        })
        self.remote_server_dev = self.Remote.create({
            'name': 'bla@example.com:dev/server',
            'repo_id': self.repo_server.id,
            'token': '123',
        })
        self.remote_addons = self.Remote.create({
            'name': 'bla@example.com:base/addons',
            'repo_id': self.repo_addons.id,
            'token': '123',
        })
        self.remote_addons_dev = self.Remote.create({
            'name': 'bla@example.com:dev/addons',
            'repo_id': self.repo_addons.id,
            'token': '123',
        })

        self.version_13 = self.Version.create({'name': '13.0'})
        self.default_config = self.env.ref('runbot.runbot_build_config_default')

        self.initial_server_commit = self.Commit.create({
            'name': 'aaaaaaa',
            'repo_id': self.repo_server.id,
            'date': '2006-12-07',
            'subject': 'New trunk',
            'author': 'purply',
            'author_email': 'puprly@somewhere.com'
        })
        self.env['ir.config_parameter'].sudo().set_param('runbot.runbot_is_base_regex', r'^((master)|(saas-)?\d+\.\d+)$')

        self.branch_server = self.Branch.create({
            'name': 'master',
            'remote_id': self.remote_server.id,
            'is_pr': False,
            'head': self.initial_server_commit.id,
        })
        self.branch_server.bundle_id # compute
        self.dev_bundle = self.Bundle.create({
            'name': 'master-dev-tri',
            'project_id': self.project.id
        })
        self.dev_branch = self.Branch.create({
            'name': 'master-dev-tri',
            'bundle_id': self.dev_bundle.id,
            'is_pr': False,
            'remote_id': self.remote_server.id,
        })
        self.dev_pr = self.Branch.create({
            'name': '1234',
            'is_pr': True,
            'remote_id': self.remote_server.id,
            'target_branch_name': self.dev_bundle.base_id.name,
            'pull_head_remote_id': self.remote_server.id,
        })
        self.dev_pr.pull_head_name = f'{self.remote_server.owner}:{self.dev_branch.name}'
        self.dev_pr.bundle_id = self.dev_bundle.id,

        self.dev_batch = self.Batch.create({
            'bundle_id': self.dev_bundle.id,
        })

        self.base_params = self.BuildParameters.create({
            'version_id': self.version_13.id,
            'project_id': self.project.id,
            'config_id': self.default_config.id,
            'create_batch_id': self.dev_batch.id,
        })

        self.trigger_server = self.Trigger.create({
            'name': 'Server trigger',
            'repo_ids': [(4, self.repo_server.id)],
            'config_id': self.default_config.id,
            'project_id': self.project.id,
        })

        self.trigger_addons = self.Trigger.create({
            'name': 'Addons trigger',
            'repo_ids': [(4, self.repo_addons.id)],
            'dependency_ids': [(4, self.repo_server.id)],
            'config_id': self.default_config.id,
            'project_id': self.project.id,
        })

        self.patchers = {}
        self.patcher_objects = {}
        self.commit_list = {}
        self.diff = ''
        self.start_patcher('git_patcher', 'odoo.addons.runbot.models.repo.Repo._git', new=self.mock_git_helper())
        self.start_patcher('hostname_patcher', 'odoo.addons.runbot.common.socket.gethostname', 'host.runbot.com')
        self.start_patcher('github_patcher', 'odoo.addons.runbot.models.repo.Remote._github', {})
        self.start_patcher('repo_root_patcher', 'odoo.addons.runbot.models.runbot.Runbot._root', '/tmp/runbot_test/static')
        self.start_patcher('makedirs', 'odoo.addons.runbot.common.os.makedirs', True)
        self.start_patcher('mkdir', 'odoo.addons.runbot.common.os.mkdir', True)
        self.start_patcher('local_pgadmin_cursor', 'odoo.addons.runbot.common.local_pgadmin_cursor', False)  # avoid to create databases
        self.start_patcher('host_local_pg_cursor', 'odoo.addons.runbot.models.host.local_pg_cursor')
        self.start_patcher('isdir', 'odoo.addons.runbot.common.os.path.isdir', True)
        self.start_patcher('isfile', 'odoo.addons.runbot.common.os.path.isfile', True)
        self.start_patcher('docker_run', 'odoo.addons.runbot.container._docker_run')
        self.start_patcher('docker_build', 'odoo.addons.runbot.container._docker_build')
        self.start_patcher('docker_ps', 'odoo.addons.runbot.container._docker_ps', [])
        self.start_patcher('docker_stop', 'odoo.addons.runbot.container._docker_stop')
        self.start_patcher('docker_get_gateway_ip', 'odoo.addons.runbot.models.build_config.docker_get_gateway_ip', None)

        self.start_patcher('repo_commit', 'odoo.addons.runbot.models.runbot.Runbot._commit', None)
        self.start_patcher('_local_cleanup_patcher', 'odoo.addons.runbot.models.build.BuildResult._local_cleanup')
        self.start_patcher('_local_pg_dropdb_patcher', 'odoo.addons.runbot.models.build.BuildResult._local_pg_dropdb')

        self.start_patcher('set_psql_conn_count', 'odoo.addons.runbot.models.host.Host.set_psql_conn_count', None)
        self.start_patcher('reload_nginx', 'odoo.addons.runbot.models.runbot.Runbot._reload_nginx', None)
        self.start_patcher('update_commits_infos', 'odoo.addons.runbot.models.batch.Batch._update_commits_infos', None)
        self.start_patcher('_local_pg_createdb', 'odoo.addons.runbot.models.build.BuildResult._local_pg_createdb', True)
        self.start_patcher('getmtime', 'odoo.addons.runbot.common.os.path.getmtime', datetime.datetime.now().timestamp())

        self.start_patcher('_get_py_version', 'odoo.addons.runbot.models.build.BuildResult._get_py_version', 3)

        def no_commit(*_args, **_kwargs):
            _logger.info('Skipping commit')

        self.patch(self.env.cr, 'commit', no_commit)


    def start_patcher(self, patcher_name, patcher_path, return_value=DEFAULT, side_effect=DEFAULT, new=DEFAULT):

        def stop_patcher_wrapper():
            self.stop_patcher(patcher_name)

        patcher = patch(patcher_path, new=new)
        if not hasattr(patcher, 'is_local'):
            res = patcher.start()
            self.addCleanup(stop_patcher_wrapper)
            self.patchers[patcher_name] = res
            self.patcher_objects[patcher_name] = patcher
            if side_effect != DEFAULT:
                res.side_effect = side_effect
            elif return_value != DEFAULT:
                res.return_value = return_value

    def stop_patcher(self, patcher_name):
        if patcher_name in self.patcher_objects:
            self.patcher_objects[patcher_name].stop()
            del self.patcher_objects[patcher_name]

    def additionnal_setup(self):
        """Helper that setup a the repos with base branches and heads"""
        self.branch_server.bundle_id.is_base = True
        initial_addons_commit = self.Commit.create({
            'name': 'cccccc',
            'repo_id': self.repo_addons.id,
            'date': '2015-03-12',
            'subject': 'Initial commit',
            'author': 'someone',
            'author_email': 'someone@somewhere.com'
        })

        self.branch_addons = self.Branch.create({
            'name': 'master',
            'remote_id': self.remote_addons.id,
            'is_pr': False,
            'head': initial_addons_commit.id,
        })
        self.assertEqual(self.branch_addons.bundle_id, self.branch_server.bundle_id)
        triggers = self.env['runbot.trigger'].search([('repo_ids', 'in', [self.repo_addons.id, self.repo_server.id])])

        self.assertEqual(triggers.repo_ids + triggers.dependency_ids, self.remote_addons.repo_id + self.remote_server.repo_id)

        batch = self.branch_addons.bundle_id._force()
        batch._prepare()


class RunbotCaseMinimalSetup(RunbotCase):

    def start_patchers(self):
        """Start necessary patchers for tests that use repo__update_batch() and batch._prepare()"""
        def counter():
            i = 100000
            while True:
                i += 1
                yield i

        # start patchers
        self.start_patcher('repo_get_fetch_head_time_patcher', 'odoo.addons.runbot.models.repo.Repo._get_fetch_head_time')
        self.patchers['repo_get_fetch_head_time_patcher'].side_effect = counter()
        self.start_patcher('repo_update_patcher', 'odoo.addons.runbot.models.repo.Repo._update')
        self.start_patcher('batch_update_commits_infos', 'odoo.addons.runbot.models.batch.Batch._update_commits_infos')
