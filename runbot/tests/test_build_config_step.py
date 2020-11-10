# -*- coding: utf-8 -*-
from unittest.mock import patch, mock_open
from odoo.exceptions import UserError
from odoo.addons.runbot.common import RunbotException
from .common import RunbotCase


class TestBuildConfigStep(RunbotCase):

    def setUp(self):
        super(TestBuildConfigStep, self).setUp()

        self.Build = self.env['runbot.build']
        self.ConfigStep = self.env['runbot.build.config.step']
        self.Config = self.env['runbot.build.config']

        server_commit = self.Commit.create({
            'name': 'dfdfcfcf0000ffffffffffffffffffffffffffff',
            'repo_id': self.repo_server.id
        })
        self.parent_build = self.Build.create({
            'params_id': self.base_params.copy({'commit_link_ids': [(0, 0, {'commit_id': server_commit.id})]}).id,
        })
        self.start_patcher('find_patcher', 'odoo.addons.runbot.common.find', 0)

    def test_config_step_create_results(self):
        """ Test child builds are taken into account"""

        config_step = self.ConfigStep.create({
            'name': 'test_step',
            'job_type': 'create_build',
            'number_builds': 2,
            'make_orphan': False,
        })

        config = self.Config.create({'name': 'test_config'})
        config_step.create_config_ids = [config.id]

        config_step._run_create_build(self.parent_build, '/tmp/essai')
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
        })

        config = self.Config.create({'name': 'test_config'})
        config_step.create_config_ids = [config.id]

        config_step._run_create_build(self.parent_build, '/tmp/essai')
        self.assertEqual(len(self.parent_build.children_ids), 2, 'Two sub-builds should have been generated')

        # check that the result will be ignored by parent build
        for child_build in self.parent_build.children_ids:
            self.assertTrue(child_build.orphan_result, 'An orphan result config step should mark the build as orphan_result')
            child_build.local_result = 'ko'

        self.assertFalse(self.parent_build.global_result)

    def test_config_step_raises(self):
        """ Test a config raises when run step position is wrong"""

        run_step = self.ConfigStep.create({
            'name': 'run_step',
            'job_type': 'run_odoo',
        })

        create_step = self.ConfigStep.create({
            'name': 'test_step',
            'job_type': 'create_build',
        })

        config = self.Config.create({'name': 'test_config'})

        # test that the run_odoo step has to be the last one
        with self.assertRaises(UserError):
            config.write({
                 'step_order_ids': [
                     (0, 0, {'sequence': 10, 'step_id': run_step.id}),
                     (0, 0, {'sequence': 15, 'step_id': create_step.id}),
                 ]
             })

        # test that the run_odoo step should be preceded by an install step
        with self.assertRaises(UserError):
            config.write({
                'step_order_ids': [
                    (0, 0, {'sequence': 15, 'step_id': run_step.id}),
                    (0, 0, {'sequence': 10, 'step_id': create_step.id}),
                ]
            })

    def test_config_step_copy(self):
        """ Test a config copy with step_order_ids """

        install_step = self.ConfigStep.create({
            'name': 'install_step',
            'job_type': 'install_odoo'
        })

        run_step = self.ConfigStep.create({
            'name': 'run_step',
            'job_type': 'run_odoo',
        })

        create_step = self.ConfigStep.create({
            'name': 'test_step',
            'job_type': 'create_build',
        })

        config = self.Config.create({'name': 'test_config'})
        StepOrder = self.env['runbot.build.config.step.order']
        # Creation order is impoortant to reproduce the Odoo copy bug/feature :-)
        StepOrder.create({'sequence': 15, 'step_id': run_step.id, 'config_id': config.id})
        StepOrder.create({'sequence': 10, 'step_id': create_step.id, 'config_id': config.id})
        StepOrder.create({'sequence': 12, 'step_id': install_step.id, 'config_id': config.id})

        dup_config = config.copy()
        self.assertEqual(dup_config.step_order_ids.mapped('step_id'), config.step_order_ids.mapped('step_id'))

    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_coverage(self, mock_checkout):
        config_step = self.ConfigStep.create({
            'name': 'coverage',
            'job_type': 'install_odoo',
            'coverage': True
        })

        def docker_run(cmd, log_path, *args, **kwargs):
            self.assertEqual(cmd.pres, [['sudo', 'pip3', 'install', '-r', 'server/requirements.txt']])
            self.assertEqual(cmd.cmd[:10], ['python3', '-m', 'coverage', 'run', '--branch', '--source', '/data/build', '--omit', '*__manifest__.py', 'server/server.py'])
            self.assertIn(['python3', '-m', 'coverage', 'html', '-d', '/data/build/coverage', '--ignore-errors'], cmd.posts)
            self.assertIn(['python3', '-m', 'coverage', 'xml', '-o', '/data/build/logs/coverage.xml', '--ignore-errors'], cmd.posts)
            self.assertEqual(log_path, 'dev/null/logpath')

        self.patchers['docker_run'].side_effect = docker_run
        config_step._run_install_odoo(self.parent_build, 'dev/null/logpath')

    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_dump(self, mock_checkout):
        config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'install_odoo',
        })

        def docker_run(cmd, log_path, *args, **kwargs):
            dest = self.parent_build.dest
            self.assertEqual(cmd.cmd[:2], ['python3', 'server/server.py'])
            self.assertEqual(cmd.finals[0], ['pg_dump', '%s-all' % dest, '>', '/data/build/logs/%s-all//dump.sql' % dest])
            self.assertEqual(cmd.finals[1], ['cp', '-r', '/data/build/datadir/filestore/%s-all' % dest, '/data/build/logs/%s-all//filestore/' % dest])
            self.assertEqual(cmd.finals[2], ['cd', '/data/build/logs/%s-all/' % dest, '&&', 'zip', '-rmq9', '/data/build/logs/%s-all.zip' % dest, '*'])
            self.assertEqual(log_path, 'dev/null/logpath')

        self.patchers['docker_run'].side_effect = docker_run

        config_step._run_install_odoo(self.parent_build, 'dev/null/logpath')

    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
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
            self.assertEqual(cmds[1].split(' server/server.py')[0], 'python3')
            tags = cmds[1].split('--test-tags ')[1].split(' ')[0]
            self.assertEqual(tags, '/module,:class.method')

        self.patchers['docker_run'].side_effect = docker_run
        config_step._run_install_odoo(self.parent_build, 'dev/null/logpath')

        config_step.enable_auto_tags = True

        def docker_run2(cmd, *args, **kwargs):
            cmds = cmd.build().split(' && ')
            self.assertEqual(cmds[1].split(' server/server.py')[0], 'python3')
            tags = cmds[1].split('--test-tags ')[1].split(' ')[0]
            self.assertEqual(tags, '/module,:class.method,-:otherclass.othertest')

        self.patchers['docker_run'].side_effect = docker_run2
        config_step._run_install_odoo(self.parent_build, 'dev/null/logpath')

    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_db_name(self, mock_checkout):
        config_step = self.ConfigStep.create({
            'name': 'default',
            'job_type': 'install_odoo',
            'custom_db_name': 'custom',
        })
        call_count = 0
        assert_db_name = 'custom'

        def docker_run(cmd, log_path, *args, **kwargs):
            db_sufgfix = cmd.cmd[cmd.index('-d')+1].split('-')[-1]
            self.assertEqual(db_sufgfix, assert_db_name)
            nonlocal call_count
            call_count += 1

        self.patchers['docker_run'].side_effect = docker_run

        config_step._run_step(self.parent_build, 'dev/null/logpath')

        assert_db_name = 'custom_build'
        parent_build_params = self.parent_build.params_id.copy({'config_data': {'db_name': 'custom_build'}})
        parent_build = self.parent_build.copy({'params_id': parent_build_params.id})
        config_step._run_step(parent_build, 'dev/null/logpath')

        config_step = self.ConfigStep.create({
            'name': 'run_test',
            'job_type': 'run_odoo',
            'custom_db_name': 'custom',
        })
        config_step._run_step(parent_build, 'dev/null/logpath')

        self.assertEqual(call_count, 3)

    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_run_python(self, mock_checkout):
        """minimal test for python steps. Also test that `-d` in cmd creates a database"""
        test_code = """cmd = build._cmd()
cmd += ['-d', 'test_database']
docker_params = dict(cmd=cmd)
        """
        config_step = self.ConfigStep.create({
            'name': 'default',
            'job_type': 'python',
            'python_code': test_code,
        })

        def docker_run(cmd, *args, **kwargs):
            run_cmd = cmd.build()
            self.assertIn('-d test_database', run_cmd)

        self.patchers['docker_run'].side_effect = docker_run
        config_step._run_step(self.parent_build, 'dev/null/logpath')
        self.patchers['docker_run'].assert_called_once()
        db = self.env['runbot.database'].search([('name', '=', 'test_database')])
        self.assertEqual(db.build_id, self.parent_build)

    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_sub_command(self, mock_checkout):
        config_step = self.ConfigStep.create({
            'name': 'default',
            'job_type': 'install_odoo',
            'sub_command': 'subcommand',
        })
        call_count = 0

        def docker_run(cmd, log_path, *args, **kwargs):
            nonlocal call_count
            sub_command = cmd.cmd[cmd.index('server/server.py')+1]
            self.assertEqual(sub_command, 'subcommand')
            call_count += 1

        self.patchers['docker_run'].side_effect = docker_run
        config_step._run_step(self.parent_build, 'dev/null/logpath')

        self.assertEqual(call_count, 1)


class TestMakeResult(RunbotCase):

    def setUp(self):
        super(TestMakeResult, self).setUp()
        self.ConfigStep = self.env['runbot.build.config.step']
        self.Config = self.env['runbot.build.config']

    @patch('odoo.addons.runbot.models.build_config.os.path.getmtime')
    @patch('odoo.addons.runbot.models.build.BuildResult._log')
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
            'params_id': self.base_params.id,
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

        # no error but build was already in warn
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
            'params_id': self.base_params.id,
        })
        build.state = 'testing'  # what ??
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

# TODO add generic test to copy_paste _run_* in a python step
