# -*- coding: utf-8 -*-
from unittest.mock import patch
from odoo.tests import common

RTE_ERROR = """FAIL: TestUiTranslate.test_admin_tour_rte_translator
Traceback (most recent call last):
  File "/data/build/odoo/addons/website/tests/test_ui.py", line 89, in test_admin_tour_rte_translator
    self.start_tour("/", 'rte_translator', login='admin', timeout=120)
  File "/data/build/odoo/odoo/tests/common.py", line 1062, in start_tour
    res = self.browser_js(url_path=url_path, code=code, ready=ready, **kwargs)
  File "/data/build/odoo/odoo/tests/common.py", line 1046, in browser_js
    self.fail('%s\n%s' % (message, error))
AssertionError: The test code "odoo.startTour('rte_translator')" failed
Tour rte_translator failed at step click language dropdown (trigger: .js_language_selector .dropdown-toggle)
"""

class TestBuildError(common.TransactionCase):

    def create_build(self, vals):
        create_vals = {
            'branch_id': self.branch.id,
            'name': 'deadbeaf0000ffffffffffffffffffffffffffff',
            'port': '1234',
            'local_result': 'ok'
        }
        create_vals.update(vals)
        return self.Build.create(create_vals)
        

    @patch('odoo.addons.runbot.models.build.runbot_build._get_params')
    def setUp(self, mock_get_params):
        super(TestBuildError, self).setUp()
        repo = self.env['runbot.repo'].create({'name': 'bla@example.com:foo/bar'})
        self.branch = self.env['runbot.branch'].create({
            'repo_id': repo.id,
            'name': 'refs/heads/master'
        })

        self.Build = self.env['runbot.build']
        self.BuildError = self.env['runbot.build.error']

    @patch('odoo.addons.runbot.models.build.runbot_build._get_params')
    def test_build_scan(self, mock_get_params):
        IrLog = self.env['ir.logging']
        ko_build = self.create_build({'local_result': 'ko'})
        ok_build = self.create_build({'local_result': 'ok'})

        log = {'message': RTE_ERROR,
               'build_id': ko_build.id,
               'level': 'ERROR',
               'type': 'server',
               'name': 'test-build-error-name',
               'path': 'test-build-error-path',
               'func': 'test-build-error-func',
               'line': 1,
        }

        # Test the build parse and ensure that an 'ok' build is not parsed
        IrLog.create(log)
        log.update({'build_id': ok_build.id})
        IrLog.create(log)
        ko_build._parse_logs()
        ok_build._parse_logs()
        build_error = self.BuildError.search([('build_ids','in', [ko_build.id])])
        self.assertIn(ko_build, build_error.build_ids, 'The parsed build should be added to the runbot.build.error')
        self.assertFalse(self.BuildError.search([('build_ids','in', [ok_build.id])]), 'A successful build should not associated to a runbot.build.error')

        # Test that build with same error is added to the errors
        ko_build_same_error = self.create_build({'local_result': 'ko'})
        log.update({'build_id': ko_build_same_error.id})
        IrLog.create(log)
        ko_build_same_error._parse_logs()
        self.assertIn(ko_build_same_error, build_error.build_ids, 'The parsed build should be added to the existing runbot.build.error')
        
        # Test that line numbers does not interfere with error recognition
        ko_build_diff_number = self.create_build({'local_result': 'ko'})
        rte_diff_numbers = RTE_ERROR.replace('89','100').replace('1062','1000').replace('1046', '4610')
        log.update({'build_id': ko_build_diff_number.id, 'message': rte_diff_numbers})
        IrLog.create(log)
        ko_build_diff_number._parse_logs()
        self.assertIn(ko_build_diff_number, build_error.build_ids, 'The parsed build with different line numbers in error should be added to the runbot.build.error')
        
        # Test that when an error re-appears after the bug has been fixed,
        # a new build error is created, with the old one linked
        build_error.active = False
        ko_build_new = self.create_build({'local_result': 'ko'})
        log.update({'build_id': ko_build_new.id})
        IrLog.create(log)
        ko_build_new._parse_logs()
        self.assertNotIn(ko_build_new, build_error.build_ids, 'The parsed build should not be added to a fixed runbot.build.error')
        new_build_error = self.BuildError.search([('build_ids','in', [ko_build_new.id])])
        self.assertIn(ko_build_new, new_build_error.build_ids, 'The parsed build with a re-apearing error should generate a new runbot.build.error')
        self.assertIn(build_error, new_build_error.error_history_ids, 'The old error should appear in history')
