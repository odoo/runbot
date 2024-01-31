import hashlib

from odoo import fields
from odoo.exceptions import ValidationError
from .common import RunbotCase

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


class TestBuildError(RunbotCase):

    def create_test_build(self, vals):
        create_vals = {
            'params_id': self.base_params.id,
            'port': '1234',
            'local_result': 'ok'
        }
        create_vals.update(vals)
        return self.Build.create(create_vals)

    def create_log(self, vals):
        log_vals = {
            'level': 'ERROR',
            'type': 'server',
            'name': 'test-build-error-name',
            'path': '/data/build/server/addons/web_studio/tests/test_ui.py',
            'func': 'test-build-error-func',
            'line': 1,
        }
        log_vals.update(vals)
        return self.IrLog.create(log_vals)


    def setUp(self):
        super(TestBuildError, self).setUp()
        self.BuildError = self.env['runbot.build.error']
        self.BuildErrorLink = self.env['runbot.build.error.link']
        self.BuildErrorTeam = self.env['runbot.team']
        self.ErrorRegex = self.env['runbot.error.regex']
        self.IrLog = self.env['ir.logging']

    def test_create_write_clean(self):

        self.ErrorRegex.create({
            'regex': '\d+',
            're_type': 'cleaning',
        })

        error_x = self.BuildError.create({
            'content': 'foo bar 242',
        })

        expected = 'foo bar %'
        expected_hash = hashlib.sha256(expected.encode()).hexdigest()
        self.assertEqual(error_x.cleaned_content, expected)
        self.assertEqual(error_x.fingerprint, expected_hash)

        # Let's ensure that the fingerprint changes if we clean with an additional regex
        self.ErrorRegex.create({
            'regex': 'bar',
            're_type': 'cleaning',
        })
        error_x.action_clean_content()
        expected = 'foo % %'
        expected_hash = hashlib.sha256(expected.encode()).hexdigest()
        self.assertEqual(error_x.cleaned_content, expected)
        self.assertEqual(error_x.fingerprint, expected_hash)

    def test_merge(self):
        build_a = self.create_test_build({'local_result': 'ko', 'local_state': 'done'})
        error_a = self.BuildError.create({'content': 'foo bar'})
        self.BuildErrorLink.create({'build_id': build_a.id, 'build_error_id': error_a.id})

        build_b = self.create_test_build({'local_result': 'ko', 'local_state': 'done'})
        error_b = self.BuildError.create({'content': 'foo bar'})
        self.BuildErrorLink.create({'build_id': build_b.id, 'build_error_id': error_b.id})

        (error_a | error_b)._merge()
        self.assertEqual(len(self.BuildError.search([('fingerprint', '=', error_a.fingerprint)])), 1)
        self.assertTrue(error_a.active, 'The first merged error should stay active')
        self.assertFalse(error_b.active, 'The second merged error should have stay deactivated')
        self.assertIn(build_a, error_a.build_error_link_ids.build_id)
        self.assertIn(build_a, error_a.build_ids)
        self.assertIn(build_b, error_a.build_error_link_ids.build_id)
        self.assertIn(build_b, error_a.build_ids)
        self.assertFalse(error_b.build_error_link_ids)
        self.assertFalse(error_b.build_ids)

        error_c = self.BuildError.create({'content': 'foo foo'})

        # let's ensure we cannot merge errors with different fingerprints
        with self.assertRaises(AssertionError):
            (error_a | error_c)._merge()

        # merge two build errors while the build <--> build_error relation already exists
        error_d = self.BuildError.create({'content': 'foo bar'})
        self.BuildErrorLink.create({'build_id': build_a.id, 'build_error_id': error_d.id})
        (error_a | error_d)._merge()
        self.assertIn(build_a, error_a.build_error_link_ids.build_id)
        self.assertIn(build_a, error_a.build_ids)
        self.assertFalse(error_d.build_error_link_ids)
        self.assertFalse(error_d.build_ids)

    def test_merge_linked(self):
        top_error = self.BuildError.create({'content': 'foo foo', 'active': False})

        build_a = self.create_test_build({'local_result': 'ko', 'local_state': 'done'})
        error_a = self.BuildError.create({'content': 'foo bar', 'parent_id': top_error.id })
        self.BuildErrorLink.create({'build_id': build_a.id, 'build_error_id': error_a.id})

        build_b = self.create_test_build({'local_result': 'ko', 'local_state': 'done'})
        error_b = self.BuildError.create({'content': 'foo bar', 'test_tags': 'footag'})
        self.BuildErrorLink.create({'build_id': build_b.id, 'build_error_id': error_b.id})

        linked_error = self.BuildError.create({'content': 'foo foo bar', 'parent_id': error_b.id})

        (error_a | error_b)._merge()
        self.assertEqual(len(self.BuildError.search([('fingerprint', '=', error_a.fingerprint)])), 1)
        self.assertTrue(error_a.active, 'The first merged error should stay active')
        self.assertFalse(error_b.active, 'The second merged error should have stay deactivated')
        self.assertIn(build_a, error_a.build_ids)
        self.assertIn(build_b, error_a.build_ids)
        self.assertFalse(error_b.build_ids)
        self.assertEqual(top_error.test_tags, 'footag')
        self.assertEqual(top_error.active, True)
        self.assertEqual(linked_error.parent_id, error_a, 'Linked errors to a merged one should be now linked to the new one')

        tagged_error = self.BuildError.create({'content': 'foo foo', 'test_tags': 'bartag'})
        (top_error | tagged_error)._merge()
        self.assertTrue(top_error.active)
        self.assertTrue(tagged_error.active, 'A differently tagged error cannot be deactivated by the merge')


    def test_build_scan(self):
        ko_build = self.create_test_build({'local_result': 'ok', 'local_state': 'testing'})
        ko_build_b = self.create_test_build({'local_result': 'ok', 'local_state': 'testing'})
        ok_build = self.create_test_build({'local_result': 'ok', 'local_state': 'running'})

        self.env['runbot.error.regex'].create({
            'regex': '^FAIL: ',
            're_type': 'cleaning',
        })

        error_team = self.BuildErrorTeam.create({
            'name': 'test-error-team',
            'path_glob': '*/test_ui.py'
        })

        # Test the build parse and ensure that an 'ok' build is not parsed
        self.create_log({'create_date': fields.Datetime.from_string('2023-08-29 00:46:21'), 'message': RTE_ERROR, 'build_id': ko_build.id})
        # As it happens that a same error could appear again in the same build, ensure that the parsing adds only one link
        self.create_log({'create_date': fields.Datetime.from_string('2023-08-29 00:48:21'), 'message': RTE_ERROR, 'build_id': ko_build.id})

        # now simulate another build with the same errors
        self.create_log({'create_date': fields.Datetime.from_string('2023-08-29 01:46:21'), 'message': RTE_ERROR, 'build_id': ko_build_b.id})
        self.create_log({'create_date': fields.Datetime.from_string('2023-08-29 01:46:21'), 'message': RTE_ERROR, 'build_id': ko_build_b.id})

        # The error also appears in a running build
        self.create_log({'create_date': fields.Datetime.from_string('2023-08-29 01:46:21'), 'message': RTE_ERROR, 'build_id': ok_build.id})

        self.assertEqual(ko_build.local_result, 'ko', 'Testing build should have gone ko after error log')
        self.assertEqual(ok_build.local_result, 'ok', 'Running build should not have gone ko after error log')

        ko_build._parse_logs()
        ko_build_b._parse_logs()
        ok_build._parse_logs()
        build_error = ko_build.build_error_ids
        self.assertTrue(build_error)
        self.assertTrue(build_error.fingerprint.startswith('af0e88f3'))
        self.assertTrue(build_error.cleaned_content.startswith('%'), 'The cleaner should have replace "FAIL: " with a "%" sign by default')
        error_link = self.env['runbot.build.error.link'].search([('build_id', '=', ko_build.id), ('build_error_id', '=', build_error.id)])
        self.assertTrue(error_link, 'An error link should exists')
        self.assertIn(ko_build, build_error.build_error_link_ids.mapped('build_id'), 'Ko build should be in build_error_link_ids')
        self.assertEqual(error_link.log_date, fields.Datetime.from_string('2023-08-29 00:46:21'))
        self.assertIn(ko_build, build_error.build_ids, 'The parsed build should be added to the runbot.build.error')
        self.assertFalse(self.BuildErrorLink.search([('build_id', '=', ok_build.id)]), 'A successful build should not be associated to a runbot.build.error')
        self.assertEqual(error_team, build_error.team_id)

        # Test that build with same error is added to the errors
        ko_build_same_error = self.create_test_build({'local_result': 'ko'})
        self.create_log({'create_date': fields.Datetime.from_string('2023-08-29 01:46:21'), 'message': RTE_ERROR, 'build_id': ko_build_same_error.id})
        ko_build_same_error._parse_logs()
        self.assertIn(ko_build_same_error, build_error.build_ids, 'The parsed build should be added to the existing runbot.build.error')

        # Test that line numbers does not interfere with error recognition
        ko_build_diff_number = self.create_test_build({'local_result': 'ko'})
        rte_diff_numbers = RTE_ERROR.replace('89', '100').replace('1062', '1000').replace('1046', '4610')
        self.create_log({'create_date': fields.Datetime.from_string('2023-08-29 01:46:21'), 'message': rte_diff_numbers, 'build_id': ko_build_diff_number.id})
        ko_build_diff_number._parse_logs()
        self.assertIn(ko_build_diff_number, build_error.build_ids, 'The parsed build with different line numbers in error should be added to the runbot.build.error')

        # Test that when an error re-appears after the bug has been fixed,
        # a new build error is created, with the old one linked
        build_error.active = False
        ko_build_new = self.create_test_build({'local_result': 'ko'})
        self.create_log({'create_date': fields.Datetime.from_string('2023-08-29 01:46:21'), 'message': RTE_ERROR, 'build_id': ko_build_new.id})
        ko_build_new._parse_logs()
        self.assertNotIn(ko_build_new, build_error.build_ids, 'The parsed build should not be added to a fixed runbot.build.error')
        new_build_error = self.BuildErrorLink.search([('build_id', '=', ko_build_new.id)]).mapped('build_error_id')
        self.assertIn(ko_build_new, new_build_error.build_ids, 'The parsed build with a re-apearing error should generate a new runbot.build.error')
        self.assertIn(build_error, new_build_error.error_history_ids, 'The old error should appear in history')

    def test_seen_date(self):
        # create all the records before the tests to evaluate compute dependencies
        build_a = self.create_test_build({'local_result': 'ok', 'local_state': 'testing'})
        first_seen_date = fields.Datetime.from_string('2023-08-29 00:46:21')
        self.create_log({'create_date': first_seen_date, 'message': RTE_ERROR, 'build_id': build_a.id})

        build_b = self.create_test_build({'local_result': 'ok', 'local_state': 'testing'})
        new_seen_date = fields.Datetime.from_string('2023-08-29 02:46:21')
        self.create_log({'create_date': new_seen_date, 'message': RTE_ERROR, 'build_id': build_b.id})

        build_c = self.create_test_build({'local_result': 'ok', 'local_state': 'testing'})
        child_seen_date = fields.Datetime.from_string('2023-09-01 12:00:00')
        self.create_log({'create_date': child_seen_date, 'message': 'Fail: foo bar error', 'build_id': build_c.id})

        build_d = self.create_test_build({'local_result': 'ok', 'local_state': 'testing'})
        new_child_seen_date = fields.Datetime.from_string('2023-09-02 12:00:00')
        self.create_log({'create_date': new_child_seen_date, 'message': 'Fail: foo bar error', 'build_id': build_d.id})

        build_a._parse_logs()
        build_error_a = build_a.build_error_ids
        self.assertEqual(build_error_a.first_seen_date, first_seen_date)
        self.assertEqual(build_error_a.first_seen_build_id, build_a)
        self.assertEqual(build_error_a.last_seen_date, first_seen_date)
        self.assertEqual(build_error_a.last_seen_build_id, build_a)

        # a new build with the same error should be the last seen
        build_b._parse_logs()
        self.assertEqual(build_error_a.last_seen_date, new_seen_date)
        self.assertEqual(build_error_a.last_seen_build_id, build_b)

        # a new build error is linked to the current one
        build_c._parse_logs()
        build_error_c = build_c.build_error_ids
        self.assertNotIn(build_c, build_error_a.children_build_ids)
        build_error_c.parent_id = build_error_a
        self.assertIn(build_c, build_error_a.children_build_ids)
        self.assertEqual(build_error_a.last_seen_date, child_seen_date)
        self.assertEqual(build_error_a.last_seen_build_id, build_c)

        # a new build appears in the linked error
        build_d._parse_logs()
        self.assertEqual(build_error_a.last_seen_date, new_child_seen_date)
        self.assertEqual(build_error_a.last_seen_build_id, build_d)

    def test_build_error_links(self):
        build_a = self.create_test_build({'local_result': 'ko'})
        build_b = self.create_test_build({'local_result': 'ko'})

        error_a = self.env['runbot.build.error'].create({
            'content': 'foo',
            'active': False  # Even a fixed error coul be linked
        })

        self.BuildErrorLink.create({'build_id': build_a.id, 'build_error_id': error_a.id})

        error_b = self.env['runbot.build.error'].create({
            'content': 'bar',
            'random': True
        })

        self.BuildErrorLink.create({'build_id': build_b.id, 'build_error_id': error_b.id})

        #  test that the random bug is parent when linking errors
        all_errors = error_a | error_b
        all_errors.action_link_errors()
        self.assertEqual(error_b.child_ids, error_a, 'Random error should be the parent')

        #  Test that changing bug resolution is propagated to children
        error_b.active = True
        self.assertTrue(error_a.active)
        error_b.active = False
        self.assertFalse(error_a.active)

        #  Test build_ids
        self.assertIn(build_b, error_b.build_ids)
        self.assertNotIn(build_a, error_b.build_ids)

        #  Test that children builds contains all builds
        self.assertIn(build_b, error_b.children_build_ids)
        self.assertIn(build_a, error_b.children_build_ids)
        self.assertEqual(error_a.build_count, 1)
        self.assertEqual(error_b.build_count, 2)

    def test_build_error_test_tags(self):
        build_a = self.create_test_build({'local_result': 'ko'})
        build_b = self.create_test_build({'local_result': 'ko'})

        error_a = self.BuildError.create({
            'content': 'foo',
            'build_ids': [(6, 0, [build_a.id])],
            'random': True,
            'active': True
        })

        error_b = self.BuildError.create({
            'content': 'bar',
            'build_ids': [(6, 0, [build_b.id])],
            'random': True,
            'active': False
        })


        error_a.test_tags = 'foo,bar'
        error_b.test_tags = 'blah'
        self.assertIn('foo', self.BuildError._test_tags_list())
        self.assertIn('bar', self.BuildError._test_tags_list())
        self.assertIn('-foo', self.BuildError._disabling_tags())
        self.assertIn('-bar', self.BuildError._disabling_tags())

        # test that test tags on fixed errors are not taken into account
        self.assertNotIn('blah', self.BuildError._test_tags_list())
        self.assertNotIn('-blah', self.BuildError._disabling_tags())

        error_a.test_tags = False
        error_b.active = True
        error_b.parent_id = error_a.id
        self.assertEqual(error_b.test_tags, False)
        self.assertEqual(self.BuildError._disabling_tags(), ['-blah',])


    def test_build_error_team_wildcards(self):
        website_team = self.BuildErrorTeam.create({
            'name': 'website_test',
            'path_glob': '*website*,-*website_sale*'
        })

        self.assertTrue(website_team.dashboard_id.exists())
        teams = self.env['runbot.team'].search(['|', ('path_glob', '!=', False), ('module_ownership_ids', '!=', False)])
        self.assertFalse(teams._get_team('/data/build/odoo/addons/web_studio/tests/test_ui.py'))
        self.assertFalse(teams._get_team('/data/build/enterprise/website_sale/tests/test_sale_process.py'))
        self.assertEqual(website_team, teams._get_team('/data/build/odoo/addons/website_crm/tests/test_website_crm'))
        self.assertEqual(website_team, teams._get_team('/data/build/odoo/addons/website/tests/test_ui'))

    def test_build_error_team_ownership(self):
        website_team = self.BuildErrorTeam.create({
            'name': 'website_test',
            'path_glob': ''
        })
        sale_team = self.BuildErrorTeam.create({
            'name': 'sale_test',
            'path_glob': ''
        })
        module_website = self.env['runbot.module'].create({
            'name': 'website_crm'
        })
        module_sale = self.env['runbot.module'].create({
            'name': 'website_sale'
        })
        self.env['runbot.module.ownership'].create({'module_id': module_website.id, 'team_id': website_team.id, 'is_fallback': True})
        self.env['runbot.module.ownership'].create({'module_id': module_sale.id, 'team_id': sale_team.id, 'is_fallback': False})
        self.env['runbot.module.ownership'].create({'module_id': module_sale.id, 'team_id': website_team.id, 'is_fallback': True})

        self.repo_server.name = 'odoo'
        self.repo_addons.name = 'enterprise'
        teams = self.env['runbot.team'].search(['|', ('path_glob', '!=', False), ('module_ownership_ids', '!=', False)])
        self.assertFalse(teams._get_team('/data/build/odoo/addons/web_studio/tests/test_ui.py'))
        self.assertEqual(website_team, teams._get_team('/data/build/odoo/addons/website_crm/tests/test_website_crm'))
        self.assertEqual(sale_team, teams._get_team('/data/build/enterprise/website_sale/tests/test_sale_process.py'))

    def test_dashboard_tile_simple(self):
        self.additionnal_setup()
        bundle = self.env['runbot.bundle'].search([('project_id', '=', self.project.id)])
        bundle.last_batch.state = 'done'
        bundle._compute_last_done_batch()  # force the recompute
        self.assertTrue(bool(bundle.last_done_batch.exists()))
        # simulate a failed build that we want to monitor
        failed_build = bundle.last_done_batch.slot_ids[0].build_id
        failed_build.global_result = 'ko'
        failed_build.flush_recordset()

        team = self.env['runbot.team'].create({'name': 'Test team'})
        dashboard = self.env['runbot.dashboard.tile'].create({
            'project_id': self.project.id,
            'category_id': bundle.last_done_batch.category_id.id,
        })

        self.assertEqual(dashboard.build_ids, failed_build)

class TestCodeOwner(RunbotCase):

    def setUp(self):
        super().setUp()
        self.cow_deb = self.env['runbot.codeowner'].create({
            'project_id' : self.project.id,
            'github_teams': 'runbot',
            'regex': '.*debian.*'
        })

        self.cow_web = self.env['runbot.codeowner'].create({
            'project_id' : self.project.id,
            'github_teams': 'website',
            'regex': '.*website.*'
        })

        self.cow_crm = self.env['runbot.codeowner'].create({
            'project_id' : self.project.id,
            'github_teams': 'crm',
            'regex': '.*crm.*'
        })

        self.cow_all = self.cow_deb | self.cow_web | self.cow_crm

    def test_codeowner_invalid_regex(self):
        with self.assertRaises(ValidationError):
            self.env['runbot.codeowner'].create({
                'project_id': self.project.id,
                'regex': '*debian.*',
                'github_teams': 'rd-test'
            })
