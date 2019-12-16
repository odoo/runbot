# -*- coding: utf-8 -*-
from unittest.mock import patch, mock_open
from odoo.tests import common
from odoo.addons.runbot.models.repo import RunbotException
from .common import RunbotCase

class TestBuildConfigStep(RunbotCase):

    def setUp(self):
        super(TestBuildConfigStep, self).setUp()
        self.repo = self.Repo.create({'name': 'bla@example.com:foo/bar', 'server_files': 'server.py'})
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
        self.start_patcher('_local_pg_createdb', 'odoo.addons.runbot.models.build.runbot_build._local_pg_createdb', True)
        self.start_patcher('_get_py_version', 'odoo.addons.runbot.models.build.runbot_build._get_py_version', 3)
        self.start_patcher('find_patcher', 'odoo.addons.runbot.common.find', 0)

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

    @patch('odoo.addons.runbot.models.build.runbot_build._checkout')
    def test_coverage(self, mock_checkout):
        config_step = self.ConfigStep.create({
            'name': 'coverage',
            'job_type': 'install_odoo',
            'coverage': True
        })

        def docker_run(cmd, log_path, *args, **kwargs):
            cmds = cmd.build().split(' && ')
            dest = self.parent_build.dest
            self.assertEqual(cmd.pres, [['sudo', 'pip3', 'install', '-r', 'bar/requirements.txt']])
            self.assertEqual(cmd.cmd[:10], ['python3', '-m', 'coverage', 'run', '--branch', '--source', '/data/build', '--omit', '*__manifest__.py', 'bar/server.py'])
            #['bar/server.py', '--addons-path', 'bar', '--no-xmlrpcs', '--no-netrpc', '-d', '08732-master-d0d0ca-coverage', '--test-enable', '--stop-after-init', '--log-level=test', '--max-cron-threads=0']
            self.assertEqual(cmd.posts, [['python3', '-m', 'coverage', 'html', '-d', '/data/build/coverage', '--ignore-errors']])
            self.assertEqual(log_path, 'dev/null/logpath')

        self.patchers['docker_run'].side_effect = docker_run

        config_step._run_odoo_install(self.parent_build, 'dev/null/logpath')

    @patch('odoo.addons.runbot.models.build.runbot_build._checkout')
    def test_dump(self, mock_checkout):
        config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'install_odoo',
        })
        def docker_run(cmd, log_path, *args, **kwargs):
            dest = self.parent_build.dest
            self.assertEqual(cmd.cmd[:2], ['python3', 'bar/server.py'])
            self.assertEqual(cmd.finals[0], ['pg_dump', '%s-all' % dest, '>', '/data/build/logs/%s-all//dump.sql' % dest])
            self.assertEqual(cmd.finals[1], ['cp', '-r', '/data/build/datadir/filestore/%s-all' % dest, '/data/build/logs/%s-all//filestore/' % dest])
            self.assertEqual(cmd.finals[2], ['cd', '/data/build/logs/%s-all/' % dest, '&&', 'zip', '-rmq9', '/data/build/logs/%s-all.zip' % dest, '*'])
            self.assertEqual(log_path, 'dev/null/logpath')

        self.patchers['docker_run'].side_effect = docker_run

        config_step._run_odoo_install(self.parent_build, 'dev/null/logpath')


    @patch('odoo.addons.runbot.models.build.runbot_build._checkout')
    def test_install_tags(self, mock_checkout):
        config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'install_odoo',
            'enable_auto_tags': False,
            'test_tags': '/module,:class.method',
        })
        self.env['runbot.build.error'].create({
            'content': 'foo',
            'random': True,
            'test_tags': ':otherclass.othertest'
        })

        def docker_run(cmd, *args, **kwargs):
            cmds = cmd.build().split(' && ')
            self.assertEqual(cmds[1].split(' bar/server.py')[0], 'python3')
            tags = cmds[1].split('--test-tags ')[1].split(' ')[0]
            self.assertEqual(tags, '/module,:class.method')

        self.patchers['docker_run'].side_effect = docker_run
        config_step._run_odoo_install(self.parent_build, 'dev/null/logpath')

        config_step.enable_auto_tags = True

        def docker_run2(cmd, *args, **kwargs):
            cmds = cmd.build().split(' && ')
            self.assertEqual(cmds[1].split(' bar/server.py')[0], 'python3')
            tags = cmds[1].split('--test-tags ')[1].split(' ')[0]
            self.assertEqual(tags, '/module,:class.method,-:otherclass.othertest')

        self.patchers['docker_run'].side_effect = docker_run2
        config_step._run_odoo_install(self.parent_build, 'dev/null/logpath')


class TestMakeResult(RunbotCase):

    def setUp(self):
        super(TestMakeResult, self).setUp()
        self.ConfigStep = self.env['runbot.build.config.step']
        self.Config = self.env['runbot.build.config']
        self.repo = self.Repo.create({'name': 'bla@example.com:foo/bar', 'server_files': 'server.py'})
        self.branch = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/heads/master'
        })

    @patch('odoo.addons.runbot.models.build_config.os.path.getmtime')
    @patch('odoo.addons.runbot.models.build.runbot_build._log')
    def test_make_result(self, mock_log, mock_getmtime):
        file_content = """
Loading stuff
odoo.stuff.modules.loading: Modules loaded.
Some post install stuff
Initiating shutdown
"""
        logs = []

        def _log(func, message, level='INFO', log_type='runbot', path='runbot'):
            logs.append((level, message))

        mock_log.side_effect = _log
        mock_getmtime.return_value = 7200

        config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'install_odoo',
            'test_tags': '/module,:class.method',
        })
        build = self.Build.create({
            'branch_id': self.branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            'port': '1234',
        })
        logs = []
        with patch('builtins.open', mock_open(read_data=file_content)):
            result = config_step._make_results(build)
        self.assertEqual(result, {'job_end': '1970-01-01 02:00:00', 'local_result': 'ok'})
        self.assertEqual(logs, [('INFO', 'Getting results for build %s' % build.dest)])
        # no shutdown
        logs = []
        file_content = """
Loading stuff
odoo.stuff.modules.loading: Modules loaded.
Some post install stuff
        """
        with patch('builtins.open', mock_open(read_data=file_content)):
            result = config_step._make_results(build)
        self.assertEqual(result, {'job_end': '1970-01-01 02:00:00', 'local_result': 'ko'})
        self.assertEqual(logs, [
            ('INFO', 'Getting results for build %s' % build.dest),
            ('ERROR', 'No "Initiating shutdown" found in logs, maybe because of cpu limit.')
        ])
        # no loaded
        logs = []
        file_content = """
Loading stuff
"""
        with patch('builtins.open', mock_open(read_data=file_content)):
            result = config_step._make_results(build)
        self.assertEqual(result, {'job_end': '1970-01-01 02:00:00', 'local_result': 'ko'})
        self.assertEqual(logs, [
            ('INFO', 'Getting results for build %s' % build.dest),
            ('ERROR', 'Modules loaded not found in logs')
        ])

        # traceback
        logs = []
        file_content = """
Loading stuff
odoo.stuff.modules.loading: Modules loaded.
Some post install stuff
2019-12-17 17:34:37,692 17 ERROR dbname path.to.test: FAIL: TestClass.test_
Traceback (most recent call last):
File "x.py", line a, in test_
    ....
Initiating shutdown
"""
        with patch('builtins.open', mock_open(read_data=file_content)):
            result = config_step._make_results(build)
        self.assertEqual(result, {'job_end': '1970-01-01 02:00:00', 'local_result': 'ko'})
        self.assertEqual(logs, [
            ('INFO', 'Getting results for build %s' % build.dest),
            ('ERROR', 'Error or traceback found in logs')
        ])

        # warning in logs
        logs = []
        file_content = """
Loading stuff
odoo.stuff.modules.loading: Modules loaded.
Some post install stuff
2019-12-17 17:34:37,692 17 WARNING dbname path.to.test: timeout exceded
Initiating shutdown
"""
        with patch('builtins.open', mock_open(read_data=file_content)):
            result = config_step._make_results(build)
        self.assertEqual(result, {'job_end': '1970-01-01 02:00:00', 'local_result': 'warn'})
        self.assertEqual(logs, [
            ('INFO', 'Getting results for build %s' % build.dest),
            ('WARNING', 'Warning found in logs')
        ])

        # no log file
        logs = []
        self.patchers['isfile'].return_value = False
        result = config_step._make_results(build)

        self.assertEqual(result, {'local_result': 'ko'})
        self.assertEqual(logs, [
            ('INFO', 'Getting results for build %s' % build.dest),
            ('ERROR', 'Log file not found at the end of test job')
        ])

        #no error but build was already in warn
        logs = []
        file_content = """
Loading stuff
odoo.stuff.modules.loading: Modules loaded.
Some post install stuff
Initiating shutdown
"""     
        self.patchers['isfile'].return_value = True
        build.local_result = 'warn'
        with patch('builtins.open', mock_open(read_data=file_content)):
            result = config_step._make_results(build)
        self.assertEqual(logs, [
            ('INFO', 'Getting results for build %s' % build.dest)
        ])
        self.assertEqual(result, {'job_end': '1970-01-01 02:00:00', 'local_result': 'warn'})

    @patch('odoo.addons.runbot.models.build_config.ConfigStep._make_tests_results')
    def test_make_python_result(self, mock_make_tests_results):
        config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'python',
            'test_tags': '/module,:class.method',
            'python_result_code': """a = 2*5\nreturn_value = {'local_result': 'ok'}"""
        })
        build = self.Build.create({
            'branch_id': self.branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            'port': '1234',
        })
        build.state = 'testing'
        self.patchers['isfile'].return_value = False
        result = config_step._make_results(build)
        self.assertEqual(result, {'local_result': 'ok'})

        # invalid result code (no return_value set)
        config_step.python_result_code = """a = 2*5\nr = {'a': 'ok'}"""
        with self.assertRaises(RunbotException):
            result = config_step._make_results(build)

        # no result defined
        config_step.python_result_code = ""
        mock_make_tests_results.return_value = {'local_result': 'warning'}
        result = config_step._make_results(build)
        self.assertEqual(result, {'local_result': 'warning'})


