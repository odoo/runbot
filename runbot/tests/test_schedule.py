# -*- coding: utf-8 -*-
import datetime
from unittest.mock import mock_open, patch

from .common import RunbotCase


class TestSchedule(RunbotCase):

    @patch('odoo.addons.runbot.models.build.os.path.getmtime')
    @patch('odoo.addons.runbot.models.build.docker_state')
    def test_schedule_mark_done(self, mock_docker_state, mock_getmtime):
        """ Test that results are set even when job_30_run is skipped """
        job_end_time = datetime.datetime.now()
        mock_getmtime.return_value = job_end_time.timestamp()  # looks wrong

        params = self.BuildParameters.create({
            'version_id': self.version_13,
            'project_id': self.project,
            'config_id': self.env.ref('runbot.runbot_build_config_default').id,
        })

        host = self.env['runbot.host'].create({'name': 'runbotxx'})  # the host needs to exists in _schedule()

        build = self.Build.create({
            'local_state': 'testing',
            'global_state': 'testing',
            'port': '1234',
            'host': host.name,
            'job_start': datetime.datetime.now(),
            'active_step': self.env.ref('runbot.runbot_build_config_step_run').id,
            'docker_start': datetime.datetime.now(),
            'params_id': params.id,
        })
        mock_docker_state.return_value = 'UNKNOWN'
        self.assertEqual(build.local_state, 'testing')
        build._schedule()
        self.assertEqual(build.local_state, 'testing')
        self.assertEqual(build.local_result, 'ok')

        build.write({'docker_start': datetime.datetime.now() - datetime.timedelta(seconds=70)})  # docker never started

        with patch('odoo.addons.runbot.common.file_open', mock_open(read_data='')):
            build._schedule()
        self.assertEqual(build.local_state, 'done')
        self.assertEqual(build.local_result, 'ko')

    def test_schedule_reap_container_per_step(self):
        """ The container of a step is inspected and removed at the end of that step,
        so that OOM kills and exit codes are logged with the step that emitted them """
        step_1 = self.env['runbot.build.config.step'].create({'name': 'step_1', 'job_type': 'python'})
        step_2 = self.env['runbot.build.config.step'].create({'name': 'step_2', 'job_type': 'python'})
        config = self.env['runbot.build.config'].create({
            'name': 'Two steps config',
            'step_order_ids': [
                (0, 0, {'step_id': step_1.id}),
                (0, 0, {'step_id': step_2.id}),
            ],
        })
        params = self.BuildParameters.create({
            'version_id': self.version_13,
            'project_id': self.project,
            'config_id': config.id,
        })
        host = self.env['runbot.host'].create({'name': 'runbotxx'})
        build = self.Build.create({
            'local_state': 'testing',
            'global_state': 'testing',
            'port': '1234',
            'host': host.name,
            'job_start': datetime.datetime.now(),
            'active_step': step_1.id,
            'docker_start': datetime.datetime.now(),
            'params_id': params.id,
        })
        step_1_container = build._get_docker_name()

        # the container is still exiting: nothing is done, the build will be rescheduled in the next loop
        self.patchers['docker_reap'].return_value = {'running': True}
        with patch('odoo.addons.runbot.models.build.docker_state', return_value='END'), \
                patch('odoo.addons.runbot.models.build_config.ConfigStep._make_results') as mock_make_results, \
                patch('odoo.addons.runbot.models.build.BuildResult._run_job') as mock_run_job:
            self.assertFalse(build._schedule())
        self.patchers['docker_reap'].assert_called_once_with(step_1_container, build._path(), 30)
        mock_make_results.assert_not_called()
        mock_run_job.assert_not_called()
        self.assertEqual(build.active_step, step_1)
        self.assertTrue(build.docker_start)
        self.assertFalse(build.docker_time)
        self.assertFalse(build.log_ids)
        self.patchers['docker_reap'].reset_mock(return_value=True)

        calls = []
        self.patchers['docker_reap'].side_effect = lambda name, build_dir, grace_time: calls.append(('reap', name)) or {'oom_killed': True, 'exit_code': 137}

        def make_results(step, build):
            calls.append(('make_results', step.name))

        def log_end(step, build):
            calls.append(('log_end', step.name))

        with patch('odoo.addons.runbot.models.build.docker_state', return_value='END'), \
                patch('odoo.addons.runbot.models.build_config.ConfigStep._make_results', autospec=True, side_effect=make_results), \
                patch('odoo.addons.runbot.models.build_config.ConfigStep._make_stats'), \
                patch('odoo.addons.runbot.models.build_config.ConfigStep._log_end', autospec=True, side_effect=log_end), \
                patch('odoo.addons.runbot.models.build.BuildResult._run_job'):
            build._schedule()

        self.assertEqual(calls, [
            ('reap', step_1_container),
            ('make_results', 'step_1'),
            ('log_end', 'step_1'),
        ], 'The container must be reaped before the results and the end of its step')
        self.assertEqual(build.active_step, step_2)
        self.assertTrue(build.oom_killed)
        messages = build.log_ids.mapped('message')
        self.assertIn('A process was killed by the OOM killer in container', messages)
        self.assertIn('The main process exited with exit code 137', messages)

        # a step without container (no docker_start) does not try to reap anything
        self.patchers['docker_reap'].reset_mock()
        build.docker_start = False
        with patch('odoo.addons.runbot.models.build.docker_state', return_value='VOID'), \
                patch('odoo.addons.runbot.models.build_config.ConfigStep._make_results'), \
                patch('odoo.addons.runbot.models.build_config.ConfigStep._make_stats'), \
                patch('odoo.addons.runbot.models.build_config.ConfigStep._log_end'), \
                patch('odoo.addons.runbot.models.build.BuildResult._run_job'):
            build._schedule()
        self.patchers['docker_reap'].assert_not_called()
