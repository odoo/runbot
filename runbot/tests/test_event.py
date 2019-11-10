# -*- coding: utf-8 -*-
from unittest.mock import patch
from odoo.tests import common
from .common import RunbotCase


class TestIrLogging(RunbotCase):

    def setUp(self):
        super(TestIrLogging, self).setUp()
        self.repo = self.Repo.create({'name': 'bla@example.com:foo/bar', 'server_files': 'server.py', 'addons_paths': 'addons,core/addons'})
        self.branch = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/heads/master'
        })
        self.Build = self.env['runbot.build']
        self.IrLogging = self.env['ir.logging']

    def simulate_log(self, build, func, message, level='INFO'):
        """ simulate ir_logging from an external build """
        dest = '%s-fake-dest' % build.id
        val = ('server', dest, 'test', level, message, 'test', '0', func)
        self.cr.execute("""
                INSERT INTO ir_logging(create_date, type, dbname, name, level, message, path, line, func)
                VALUES (NOW() at time zone 'UTC', %s, %s, %s, %s, %s, %s, %s, %s)
            """, val)

    def test_ir_logging(self):
        build = self.create_build({
            'branch_id': self.branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            'port': '1234',
            'active_step': self.env.ref('runbot.runbot_build_config_step_test_all').id,
        })

        build.log_counter = 10

        #  Test that an ir_logging is created and a the trigger set the build_id
        self.simulate_log(build, 'test function', 'test message')
        log_line = self.IrLogging.search([('func', '=', 'test function'), ('message', '=', 'test message'), ('level', '=', 'INFO')])
        self.assertEqual(len(log_line), 1, "A build log event should have been created")
        self.assertEqual(log_line.build_id, build)
        self.assertEqual(log_line.active_step_id, self.env.ref('runbot.runbot_build_config_step_test_all'), 'The active step should be set on the log line')

        #  Test that a warn log line sets the build in warn
        self.simulate_log(build, 'test function', 'test message', level='WARNING')
        build.invalidate_cache()
        self.assertEqual(build.triggered_result, 'warn', 'A warning log should sets the build in warn')

        #  Test that a error log line sets the build in ko
        self.simulate_log(build, 'test function', 'test message', level='ERROR')
        build.invalidate_cache()
        self.assertEqual(build.triggered_result, 'ko', 'An error log should sets the build in ko')
        self.assertEqual(7, build.log_counter, 'server lines should decrement the build log_counter')

        build.log_counter = 10

        # Test the log limit
        for i in range(11):
            self.simulate_log(build, 'limit function', 'limit message')
        log_lines = self.IrLogging.search([('build_id', '=', build.id), ('type', '=', 'server'), ('func', '=', 'limit function'), ('message', '=', 'limit message'), ('level', '=', 'INFO')])
        self.assertGreater(len(log_lines), 7, 'Trigger should have created logs with appropriate build id')
        self.assertLess(len(log_lines), 10, 'Trigger should prevent insert more lines of logs than log_counter')
        last_log_line = self.IrLogging.search([('build_id', '=', build.id)], order='id DESC', limit=1)
        self.assertIn('Log limit reached', last_log_line.message, 'Trigger should modify last log message')

        # Test that the _log method is still able to add logs
        build._log('runbot function', 'runbot message')
        log_lines = self.IrLogging.search([('type', '=', 'runbot'), ('name', '=', 'odoo.runbot'), ('func', '=', 'runbot function'), ('message', '=', 'runbot message'), ('level', '=', 'INFO')])
        self.assertEqual(len(log_lines), 1, '_log should be able to add logs from the runbot')
