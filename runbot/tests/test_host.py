import logging

from .common import RunbotCase

from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)

def fetch_local_logs_return_value(nb_logs=10, message='', log_type='server', level='INFO', build_dest='1234567-master-all'):

    log_date = datetime(2022, 8, 17, 21, 55)
    logs = []
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
        log_date += timedelta(seconds=20)
    return logs

class TestHost(RunbotCase):

    def setUp(self):
        super().setUp()
        self.test_host = self.env['runbot.host'].create({'name': 'test_host'})
        self.server_commit = self.Commit.create({
            'name': 'dfdfcfcf0000ffffffffffffffffffffffffffff',
            'repo_id': self.repo_server.id
        })

        self.addons_commit = self.Commit.create({
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            'repo_id': self.repo_addons.id,
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

    def test_build_logs(self):

        build = self.Build.create({
            'params_id': self.server_params.id,
            'port': '1234567',
            'active_step': self.env.ref('runbot.runbot_build_config_step_test_all').id,
            'log_counter': 20,
        })

        # check that local logs are inserted in leader ir.logging
        logs = fetch_local_logs_return_value(build_dest=build.dest)
        self.start_patcher('fetch_local_logs', 'odoo.addons.runbot.models.host.Host._fetch_local_logs', logs)
        self.test_host._process_logs()
        self.patchers['host_local_pg_cursor'].assert_called()
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
        self.test_host._process_logs()
        self.patchers['host_local_pg_cursor'].assert_called()
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
        self.test_host._process_logs()
        self.patchers['host_local_pg_cursor'].assert_called()
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
        self.test_host._process_logs()
        self.patchers['host_local_pg_cursor'].assert_called()
