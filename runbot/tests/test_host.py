import logging

from unittest.mock import call, MagicMock

from .common import RunbotCase

from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)

def fetch_local_logs_return_value(nb_logs=10, message='', log_type='server', level='INFO', build_dest='1234567-master-all'):

    log_date = datetime(2022, 8, 17, 21, 55)
    logs = []
    cleanups = []
    for i in range(nb_logs):
        logs += [{
            'id': i,
            'create_date': log_date,
            'name': 'odoo.modules.loading',
            'level': level,
            'dbname': build_dest,
            'func': 'runbot',
            'path': '/data/build/odoo/odoo/netsvc.py',
            'line': '274',
            'type': log_type,
            'message': '75 modules loaded in 0.92s, 717 queries (+1 extra)' if message == '' else message,
        }]
        cleanups.append(MagicMock())
        log_date += timedelta(seconds=20)
    return (logs, cleanups)

class TestHost(RunbotCase):

    def setUp(self):
        super().setUp()
        self.test_host = self.env['runbot.host'].create({'name': 'test_host'})
        self.server_commit = self.Commit.create({
            'name': 'dfdfcfcf0000ffffffffffffffffffffffffffff',
            'tree_hash': '0dfdfcfcf0000fffffffffffffffffffffffffff',
            'repo_id': self.repo_odoo.id
        })

        self.addons_commit = self.Commit.create({
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            'tree_hash': '0d0d0caca0000fffffffffffffffffffffffffff',
            'repo_id': self.repo_enterprise.id,
        })

        self.server_params = self.base_params.copy({'commit_link_ids': [
            (0, 0, {'commit_id': self.server_commit.id})
        ]})

        self.addons_params = self.base_params.copy({'commit_link_ids': [
            (0, 0, {'commit_id': self.server_commit.id}),
            (0, 0, {'commit_id': self.addons_commit.id})
        ]})

        self.start_patcher('find_patcher', 'odoo.addons.runbot.common.find', 0)
        self.start_patcher('host_bootstrap', 'odoo.addons.runbot.models.host.Host._bootstrap', None)
        self.start_patcher('docker_remove_container', 'odoo.addons.runbot.container._docker_remove_container')

    def _get_mock_container(self, name, running=False, oom_killed=False, exit_code=0):
        container = MagicMock()
        container.name = name
        container.attrs = {'State': {'Running': running, 'OOMKilled': oom_killed, 'ExitCode': exit_code}}
        return container

    def _create_test_build(self, local_state='done'):
        return self.Build.create({
            'params_id': self.server_params.id,
            'port': '1234',
            'local_state': local_state,
        })

    def test_docker_cleanup(self):

        # containers not managed because not dest format
        self.patchers['docker_ps'].return_value = [
            self._get_mock_container('not-a-dest', running=True),
            self._get_mock_container('1234-test', running=True),
        ]
        self.Runbot._docker_cleanup()
        self.patchers['docker_stop'].assert_not_called()
        self.patchers['docker_remove_container'].assert_not_called()

        # container in dest format but without any matching build is silently skipped
        self.patchers['docker_ps'].return_value = [self._get_mock_container('9999999999-20-0', running=True)]
        self.Runbot._docker_cleanup()
        self.patchers['docker_stop'].assert_not_called()
        self.patchers['docker_remove_container'].assert_not_called()

        # build done with a running container: the container must be stopped but not removed
        done_build = self._create_test_build(local_state='done')
        self.patchers['docker_ps'].return_value = [self._get_mock_container(done_build.dest, running=True)]
        self.Runbot._docker_cleanup()
        self.patchers['docker_stop'].assert_called_once_with(done_build.dest, done_build._path())
        self.patchers['docker_remove_container'].assert_not_called()

        # build done with a stopped container exiting cleanly: removed without any log
        self.patchers['docker_stop'].reset_mock()
        self.patchers['docker_ps'].return_value = [self._get_mock_container(done_build.dest, running=False, exit_code=0)]
        self.Runbot._docker_cleanup()
        self.patchers['docker_stop'].assert_not_called()
        self.patchers['docker_remove_container'].assert_called_once_with(done_build.dest)
        self.assertFalse(done_build.oom_killed)
        self.assertFalse(done_build.log_ids)

        # build done with a stopped container exiting with an error: removed and a log is created
        self.patchers['docker_remove_container'].reset_mock()
        self.patchers['docker_ps'].return_value = [self._get_mock_container(done_build.dest, running=False, exit_code=1)]
        self.Runbot._docker_cleanup()
        self.patchers['docker_remove_container'].assert_called_once_with(done_build.dest)
        self.assertIn('The main process exited with exit code 1', done_build.log_ids.mapped('message'))
        done_build.log_ids.unlink()

        # build done with a stopped container OOM killed: flag the build, log it and remove
        self.patchers['docker_remove_container'].reset_mock()
        self.patchers['docker_ps'].return_value = [self._get_mock_container(done_build.dest, running=False, oom_killed=True, exit_code=0)]
        self.Runbot._docker_cleanup()
        self.patchers['docker_remove_container'].assert_called_once_with(done_build.dest)
        self.assertTrue(done_build.oom_killed)
        self.assertIn('A process was killed by the OOM killer in container', done_build.log_ids.mapped('message'))

        # build not done: the container is left untouched when running
        self.patchers['docker_stop'].reset_mock()
        self.patchers['docker_remove_container'].reset_mock()
        running_build = self._create_test_build(local_state='running')
        self.patchers['docker_ps'].return_value = [self._get_mock_container(running_build.dest, running=True)]
        self.Runbot._docker_cleanup()
        self.patchers['docker_stop'].assert_not_called()
        self.patchers['docker_remove_container'].assert_not_called()

    def test_build_logs(self):

        build = self.Build.create({
            'params_id': self.server_params.id,
            'port': '1234567',
            'active_step': self.env.ref('runbot.runbot_build_config_step_test_all').id,
            'log_counter': 20,
        })

        # check that local logs are inserted in leader ir.logging
        logs = fetch_local_logs_return_value(build_dest=build.dest)
        self.patchers['fetch_local_logs'].return_value = logs
        self.test_host._process_logs(build)
        logs[1][0].assert_called()
        self.assertEqual(
            self.env['ir.logging'].search_count([
                ('build_id', '=', build.id),
                ('active_step_id', '=', self.env.ref('runbot.runbot_build_config_step_test_all').id)
            ]),
            10,
        )

        # check that a warn log sets the build in warning
        logs = fetch_local_logs_return_value(nb_logs=1, build_dest=build.dest, level='WARNING')
        self.patchers['fetch_local_logs'].return_value = logs
        self.test_host._process_logs(build)
        logs[1][0].assert_called()
        self.assertEqual(
            self.env['ir.logging'].search_count([
                ('build_id', '=', build.id),
                ('active_step_id', '=', self.env.ref('runbot.runbot_build_config_step_test_all').id),
                ('level', '=', 'WARNING')
            ]),
            1,
        )
        self.assertEqual(build.local_result, 'warn', 'A warning log should sets the build in warn')

        # now check that error logs sets the build in ko
        logs = fetch_local_logs_return_value(nb_logs=1, build_dest=build.dest, level='ERROR')
        self.patchers['fetch_local_logs'].return_value = logs
        self.test_host._process_logs(build)
        logs[1][0].assert_called()
        self.assertEqual(
            self.env['ir.logging'].search_count([
                ('build_id', '=', build.id),
                ('active_step_id', '=', self.env.ref('runbot.runbot_build_config_step_test_all').id),
                ('level', '=', 'ERROR')
            ]),
            1,
        )
        self.assertEqual(build.local_result, 'ko', 'An error log should sets the build in ko')

        build.log_counter = 10
        # Test log limit
        logs = fetch_local_logs_return_value(nb_logs=11, message='test log limit', build_dest=build.dest)
        self.patchers['fetch_local_logs'].return_value = logs
        self.test_host._process_logs(build)
        logs[1][0].assert_called()

    def test_docker_builder_existing_image(self):
        self.start_patcher('build_patcher', 'odoo.addons.runbot.models.docker.Dockerfile._build')

        # deactivate DockerDefault to avoid test pollution
        self.env.ref('runbot.docker_default').active = False

        icp = self.env['ir.config_parameter']
        icp.set_param('runbot.docker_registry_host_id', self.test_host.id)
        self.test_host.is_registry = True
        icp.set_param('runbot.docker_registry_url', 'registryhost_nowhere')
        dockerfile = self.env['runbot.dockerfile'].create({
            'name': 'DockerTest',
            'to_build': True,
            'image_identifier': 'current',
            'image_future_identifier': 'current',
            'auto_sync': False,
        })

        self.assertEqual(dockerfile.image_tag, 'odoo:DockerTest')
        self.assertEqual(dockerfile.image_future_tag, 'odoo:DockerTest.future')

        self.patchers['build_patcher'].side_effect = lambda x: False  # simulate a build failure
        self.test_host._docker_update_images()

        # For the first build there is no previous identifier
        # As the docker build is in failure, it's not tagged to future
        expected_docker_tag_calls = [
            call(False, 'odoo:DockerTest.previous'),
            call('current', 'odoo:DockerTest'),
        ]

        self.patchers['docker_tag'].assert_has_calls(expected_docker_tag_calls)

        self.assertEqual(dockerfile.image_future_identifier, 'current')

        def build_success(arg):
            return 'future'
        self.patchers['build_patcher'].side_effect = build_success  # now simulate a success
        self.patchers['docker_tag'].reset_mock()
        self.test_host._docker_update_images()

        self.assertEqual(dockerfile.image_future_identifier, 'future')

        expected_docker_tag_calls = [
            call(False, 'odoo:DockerTest.previous'),
            call('current', 'odoo:DockerTest'),
            call('future', 'odoo:DockerTest.future')
        ]

        self.patchers['docker_tag'].assert_has_calls(expected_docker_tag_calls)

        expected_push_calls = [
            call('odoo:DockerTest', 'registryhost_nowhere'),
            call('odoo:DockerTest.future', 'registryhost_nowhere')
        ]

        self.patchers['docker_push'].assert_has_calls(expected_push_calls)
        self.patchers['docker_pull'].assert_not_called()
