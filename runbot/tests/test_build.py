# -*- coding: utf-8 -*-
import datetime

from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from .common import RunbotCase, RunbotCaseMinimalSetup


def rev_parse(repo, branch_name):
    """
    simulate a rev parse by returning a fake hash of form
    'rp_odoo-dev/enterprise_saas-12.2__head'
    should be overwitten if a pr head should match a branch head
    """
    head_hash = 'rp_%s_%s_head' % (repo.name.split(':')[1], branch_name.split('/')[-1])
    return head_hash


class TestBuildParams(RunbotCaseMinimalSetup):

    def setUp(self):
        super(TestBuildParams, self).setUp()

    def test_params(self):

        server_commit = self.Commit.create({
            'name': 'dfdfcfcf0000ffffffffffffffffffffffffffff',
            'repo_id': self.repo_server.id
        })

        params = self.BuildParameters.create({
            'version_id': self.version_13.id,
            'project_id': self.project.id,
            'config_id': self.default_config.id,
            'commit_link_ids': [
                (0, 0, {'commit_id': server_commit.id})
            ],
            'config_data': {'foo': 'bar'}
        })

        # test that when the same params does not create a new record
        same_params = self.BuildParameters.create({
            'version_id': self.version_13.id,
            'project_id': self.project.id,
            'config_id': self.default_config.id,
            'commit_link_ids': [
                (0, 0, {'commit_id': server_commit.id})
            ],
            'config_data': {'foo': 'bar'}
        })

        self.assertEqual(params.fingerprint, same_params.fingerprint)
        self.assertEqual(params.id, same_params.id)

        # test that params cannot be overwitten
        with self.assertRaises(UserError):
            params.write({'modules': 'bar'})

        # Test that a copied param without changes does not create a new record
        copied_params = params.copy()
        self.assertEqual(copied_params.id, params.id)

        # Test copy with a parameter change
        other_commit = self.Commit.create({
            'name': 'deadbeef0000ffffffffffffffffffffffffffff',
            'repo_id': self.repo_server.id
        })

        copied_params = params.copy({
            'commit_link_ids': [
                (0, 0, {'commit_id': other_commit.id})
            ]
        })
        self.assertNotEqual(copied_params.id, params.id)

    def test_trigger_build_config(self):
        """Test that a build gets the build config from the trigger"""
        self.additionnal_setup()
        self.start_patchers()

        self.trigger_server.description = expected_description = "A nice trigger description"

        # A commit is found on the dev remote
        branch_a_name = 'master-test-something'
        self.push_commit(self.remote_server_dev, branch_a_name, 'nice subject', sha='d0d0caca')

        # batch preparation
        self.repo_server._update_batches()

        # prepare last_batch
        bundle = self.env['runbot.bundle'].search([('name', '=', branch_a_name), ('project_id', '=', self.project.id)])
        bundle.last_batch._prepare()
        build_slot = bundle.last_batch.slot_ids.filtered(lambda rec: rec.trigger_id == self.trigger_server)
        self.assertEqual(build_slot.build_id.params_id.config_id, self.trigger_server.config_id)
        self.assertEqual(build_slot.build_id.description, expected_description, "A build description should reflect the trigger description")

    def test_custom_trigger_config(self):
        """Test that a bundle with a custom trigger creates a build with appropriate config"""
        self.additionnal_setup()
        self.start_patchers()

        # A commit is found on the dev remote
        branch_a_name = 'master-test-something'
        self.push_commit(self.remote_server_dev, branch_a_name, 'nice subject', sha='d0d0caca')
        # batch preparation
        self.repo_server._update_batches()

        # create a custom config and a new trigger
        custom_config = self.env['runbot.build.config'].create({'name': 'A Custom Config'})

        # create a custom trigger for the bundle
        bundle = self.Bundle.search([('name', '=', branch_a_name), ('project_id', '=', self.project.id)])

        # create a custom trigger with the custom config linked to the bundle
        self.env['runbot.bundle.trigger.custom'].create({
            'trigger_id': self.trigger_server.id,
            'bundle_id': bundle.id,
            'config_id': custom_config.id
        })

        bundle.last_batch._prepare()
        build_slot = bundle.last_batch.slot_ids.filtered(lambda rec: rec.trigger_id == self.trigger_server)
        self.assertEqual(build_slot.build_id.params_id.config_id, custom_config)


class TestBuildResult(RunbotCase):

    def setUp(self):
        super(TestBuildResult, self).setUp()

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

    def test_base_fields(self):

        build = self.Build.create({
            'params_id': self.server_params.id,
            'port': '1234'
        })

        self.assertEqual(build.dest, '%05d-13-0' % build.id)

        # Test domain compute with fqdn and ir.config_parameter
        self.env['ir.config_parameter'].sudo().set_param('runbot.runbot_nginx', False)
        self.patchers['fqdn_patcher'].return_value = 'runbot98.nowhere.org'
        self.env['ir.config_parameter'].sudo().set_param('runbot.runbot_domain', False)
        self.assertEqual(build.domain, 'runbot98.nowhere.org:1234')
        self.env['ir.config_parameter'].set_param('runbot.runbot_domain', 'runbot99.example.org')
        build._compute_domain()
        self.assertEqual(build.domain, 'runbot99.example.org:1234')

        # test json stored _data field and data property
        #self.assertEqual(build.params_id.config_data, {})
        #build.params_id.config_data = {'restore_url': 'foobar'}
        #self.assertEqual(build.params_id.config_data, {'restore_url': 'foobar'})
        #build.params_id.config_data['test_info'] = 'dummy'
        #self.assertEqual(build.params_id.config_data, {"restore_url": "foobar", "test_info": "dummy"})
        #del build.params_id.config_data['restore_url']
        #self.assertEqual(build.params_id.config_data, {"test_info": "dummy"})

        other = self.Build.create({
            'params_id': self.server_params.id,
            'local_result': 'ko'
        })

        other.write({'local_result': 'ok'})
        self.assertEqual(other.local_result, 'ko')

        # test a bulk write, that one cannot change from 'ko' to 'ok'
        builds = self.Build.browse([build.id, other.id])
        with self.assertRaises(ValidationError):
            builds.write({'local_result': 'ok'})

    def test_markdown_description(self):
        build = self.Build.create({
            'params_id': self.server_params.id,
            'description': 'A nice **description**'
        })
        self.assertEqual(build.md_description, 'A nice <strong>description</strong>')

        build.description = "<script>console.log('foo')</script>"
        self.assertEqual(build.md_description, "&lt;script&gt;console.log('foo')&lt;/script&gt;")

    @patch('odoo.addons.runbot.models.build.BuildResult._get_available_modules')
    def test_filter_modules(self, mock_get_available_modules):
        """ test module filtering """

        build = self.Build.create({
            'params_id': self.addons_params.id,
        })

        mock_get_available_modules.return_value = {
            self.repo_server: ['good_module', 'bad_module', 'other_good', 'l10n_be', 'hw_foo', 'hwgood', 'hw_explicit'],
            self.repo_addons: ['other_mod_1', 'other_mod_2'],
        }

        self.repo_server.modules = '-bad_module,-hw_*,hw_explicit,-l10n_*'
        self.repo_addons.modules = '-*'

        modules_to_test = build._get_modules_to_test(modules_patterns='')
        self.assertEqual(modules_to_test, sorted(['good_module', 'hwgood', 'other_good', 'hw_explicit']))

        modules_to_test = build._get_modules_to_test(modules_patterns='-*, l10n_be')
        self.assertEqual(modules_to_test, sorted(['l10n_be']))
        modules_to_test = build._get_modules_to_test(modules_patterns='l10n_be')
        self.assertEqual(modules_to_test, sorted(['good_module', 'hwgood', 'other_good', 'hw_explicit', 'l10n_be']))
        # star to get all available mods
        modules_to_test = build._get_modules_to_test(modules_patterns='*, -hw_*, hw_explicit')
        self.assertEqual(modules_to_test, sorted(['good_module', 'bad_module', 'other_good', 'l10n_be', 'hwgood', 'hw_explicit', 'other_mod_1', 'other_mod_2']))

    def test_build_cmd_log_db(self, ):
        """ test that the logdb connection URI is taken from the .odoorc file """
        uri = 'postgres://someone:pass@somewhere.com/db'
        self.env['ir.config_parameter'].sudo().set_param("runbot.runbot_logdb_uri", uri)

        build = self.Build.create({
            'params_id': self.server_params.id,
        })
        cmd = build._cmd(py_version=3)
        self.assertIn('log_db = %s' % uri, cmd.get_config())

    def test_build_cmd_server_path_no_dep(self):
        """ test that the server path and addons path """
        build = self.Build.create({
            'params_id': self.server_params.id,
        })
        cmd = build._cmd(py_version=3)
        self.assertEqual('python3', cmd[0])
        self.assertEqual('server/server.py', cmd[1])
        self.assertIn('--addons-path', cmd)
        # TODO fix the _get_addons_path and/or _docker_source_folder
        # addons_path_pos = cmd.index('--addons-path') + 1
        # self.assertEqual(cmd[addons_path_pos], 'bar/addons,bar/core/addons')

    def test_build_cmd_server_path_with_dep(self):
        """ test that the server path and addons path are correct"""

        def is_file(file):
            self.assertIn(file, [
                '/tmp/runbot_test/static/sources/addons/d0d0caca0000ffffffffffffffffffffffffffff/requirements.txt',
                '/tmp/runbot_test/static/sources/server/dfdfcfcf0000ffffffffffffffffffffffffffff/requirements.txt',
                '/tmp/runbot_test/static/sources/server/dfdfcfcf0000ffffffffffffffffffffffffffff/server.py',
                '/tmp/runbot_test/static/sources/server/dfdfcfcf0000ffffffffffffffffffffffffffff/openerp/tools/config.py'
            ])
            if file == '/tmp/runbot_test/static/sources/addons/d0d0caca0000ffffffffffffffffffffffffffff/requirements.txt':
                return False
            return True

        def is_dir(file):
            paths = [
                'sources/server/dfdfcfcf0000ffffffffffffffffffffffffffff/addons',
                'sources/server/dfdfcfcf0000ffffffffffffffffffffffffffff/core/addons',
                'sources/addons/d0d0caca0000ffffffffffffffffffffffffffff'
            ]
            self.assertTrue(any([path in file for path in paths]))  # checking that addons path existence check looks ok
            return True

        self.patchers['isfile'].side_effect = is_file
        self.patchers['isdir'].side_effect = is_dir

        build = self.Build.create({
            'params_id': self.addons_params.id,
        })

        cmd = build._cmd(py_version=3)
        self.assertIn('--addons-path', cmd)
        addons_path_pos = cmd.index('--addons-path') + 1
        self.assertEqual(cmd[addons_path_pos], 'server/addons,server/core/addons,addons')
        self.assertEqual('server/server.py', cmd[1])
        self.assertEqual('python3', cmd[0])

    def test_build_gc_date(self):
        """ test build gc date and gc_delay"""
        build = self.Build.create({
            'params_id': self.server_params.id,
            'local_state': 'done'
        })

        child_build = self.Build.create({
            'params_id': self.server_params.id,
            'parent_id': build.id,
            'local_state': 'done'
        })

        # verify that the gc_day is set 30 days later (29 days since we should be a few microseconds later)
        delta = fields.Datetime.from_string(build.gc_date) - datetime.datetime.now()
        self.assertEqual(delta.days, 29)
        child_delta = fields.Datetime.from_string(child_build.gc_date) - datetime.datetime.now()
        self.assertEqual(child_delta.days, 14)

        # Keep child build ten days more
        child_build.gc_delay = 10
        child_delta = fields.Datetime.from_string(child_build.gc_date) - datetime.datetime.now()
        self.assertEqual(child_delta.days, 24)

        # test the real _local_cleanup method
        self.stop_patcher('_local_cleanup_patcher')
        self.start_patcher('build_local_pgadmin_cursor_patcher', 'odoo.addons.runbot.models.build.local_pgadmin_cursor')
        self.start_patcher('build_os_listdirr_patcher', 'odoo.addons.runbot.models.build.os.listdir')
        dbname = '%s-foobar' % build.dest
        self.start_patcher('list_local_dbs_patcher', 'odoo.addons.runbot.models.build.list_local_dbs', return_value=[dbname])

        build._local_cleanup()
        self.assertFalse(self.patchers['_local_pg_dropdb_patcher'].called)
        build.job_end = datetime.datetime.now() - datetime.timedelta(days=31)
        build._local_cleanup()
        self.patchers['_local_pg_dropdb_patcher'].assert_called_with(dbname)

    @patch('odoo.addons.runbot.models.build._logger')
    def test_build_skip(self, mock_logger):
        """test build is skipped"""
        build = self.Build.create({
            'params_id': self.server_params.id,
            'port': '1234',
        })
        build._skip()
        self.assertEqual(build.local_state, 'done')
        self.assertEqual(build.local_result, 'skipped')

        other_build = self.Build.create({
            'params_id': self.server_params.id,
            'port': '1234',
        })
        other_build._skip(reason='A good reason')
        self.assertEqual(other_build.local_state, 'done')
        self.assertEqual(other_build.local_result, 'skipped')
        log_first_part = '%s skip %%s' % (other_build.dest)
        mock_logger.info.assert_called_with(log_first_part, 'A good reason')

    def test_children(self):
        build1 = self.Build.create({
            'params_id': self.server_params.id,
        })
        build1_1 = self.Build.create({
            'params_id': self.server_params.id,
            'parent_id': build1.id,
        })
        build1_2 = self.Build.create({
            'params_id': self.server_params.id,
            'parent_id': build1.id,
        })
        build1_1_1 = self.Build.create({
            'params_id': self.server_params.id,
            'parent_id': build1_1.id,
        })
        build1_1_2 = self.Build.create({
            'params_id': self.server_params.id,
            'parent_id': build1_1.id,
        })

        def assert_state(global_state, build):
            self.assertEqual(build.global_state, global_state)

        assert_state('pending', build1)
        assert_state('pending', build1_1)
        assert_state('pending', build1_2)
        assert_state('pending', build1_1_1)
        assert_state('pending', build1_1_2)

        build1.local_state = 'testing'
        build1_1.local_state = 'testing'
        build1.local_state = 'done'
        build1_1.local_state = 'done'

        assert_state('waiting', build1)
        assert_state('waiting', build1_1)
        assert_state('pending', build1_2)
        assert_state('pending', build1_1_1)
        assert_state('pending', build1_1_2)

        build1_1_1.local_state = 'testing'

        assert_state('waiting', build1)
        assert_state('waiting', build1_1)
        assert_state('pending', build1_2)
        assert_state('testing', build1_1_1)
        assert_state('pending', build1_1_2)

        build1_2.local_state = 'testing'

        assert_state('waiting', build1)
        assert_state('waiting', build1_1)
        assert_state('testing', build1_2)
        assert_state('testing', build1_1_1)
        assert_state('pending', build1_1_2)

        build1_2.local_state = 'testing'  # writing same state a second time

        assert_state('waiting', build1)
        assert_state('waiting', build1_1)
        assert_state('testing', build1_2)
        assert_state('testing', build1_1_1)
        assert_state('pending', build1_1_2)

        build1_1_2.local_state = 'done'
        build1_1_1.local_state = 'done'
        build1_2.local_state = 'done'

        assert_state('done', build1)
        assert_state('done', build1_1)
        assert_state('done', build1_2)
        assert_state('done', build1_1_1)
        assert_state('done', build1_1_2)


class TestGc(RunbotCaseMinimalSetup):

    def test_repo_gc_testing(self):
        """ test that builds are killed when room is needed on a host """

        self.additionnal_setup()

        self.start_patchers()

        host = self.env['runbot.host'].create({
            'name': 'runbot_xxx',
            'nb_worker': 2
        })

        # A commit is found on the dev remote
        branch_a_name = 'master-test-something'
        self.push_commit(self.remote_server_dev, branch_a_name, 'nice subject', sha='d0d0caca')

        # batch preparation
        self.repo_server._update_batches()

        # prepare last_batch
        bundle_a = self.env['runbot.bundle'].search([('name', '=', branch_a_name)])
        bundle_a.last_batch._prepare()

        # now we should have a build in pending state in the bundle
        self.assertEqual(len(bundle_a.last_batch.slot_ids), 2)
        build_a = bundle_a.last_batch.slot_ids[0].build_id
        self.assertEqual(build_a.global_state, 'pending')

        # now another commit is found in another branch
        branch_b_name = 'master-test-other-thing'
        self.push_commit(self.remote_server_dev, branch_b_name, 'other subject', sha='cacad0d0')
        self.repo_server._update_batches()
        bundle_b = self.env['runbot.bundle'].search([('name', '=', branch_b_name)])
        bundle_b.last_batch._prepare()

        build_b = bundle_b.last_batch.slot_ids[0].build_id

        # the two builds are starting tests on two different hosts
        build_a.write({'local_state': 'testing', 'host': host.name})
        build_b.write({'local_state': 'testing', 'host': 'runbot_yyy'})

        # no room needed, verify that nobody got killed
        self.Runbot._gc_testing(host)
        self.assertFalse(build_a.requested_action)
        self.assertFalse(build_b.requested_action)

        # a new commit is pushed on branch_a
        self.push_commit(self.remote_server_dev, branch_a_name, 'new subject', sha='d0cad0ca')
        self.repo_server._update_batches()
        bundle_a = self.env['runbot.bundle'].search([('name', '=', branch_a_name)])
        bundle_a.last_batch._prepare()
        build_a_last = bundle_a.last_batch.slot_ids[0].build_id
        self.assertEqual(build_a_last.local_state, 'pending')
        self.assertTrue(build_a.killable, 'The previous build in the batch should be killable')

        # the build_b create a child build
        children_b = self.Build.create({
            'params_id': build_b.params_id.copy().id,
            'parent_id': build_b.id,
            'build_type': build_b.build_type,
        })

        # no room needed, verify that nobody got killed
        self.Runbot._gc_testing(host)
        self.assertFalse(build_a.requested_action)
        self.assertFalse(build_b.requested_action)
        self.assertFalse(build_a_last.requested_action)
        self.assertFalse(children_b.requested_action)

        # now children_b starts on runbot_xxx
        children_b.write({'local_state': 'testing', 'host': host.name})

        # we are  now in a situation where there is no more room on runbot_xxx
        # and there is a pending build: build_a_last
        # so we need to make room
        self.Runbot._gc_testing(host)

        # the killable build should have been marked to be killed
        self.assertEqual(build_a.requested_action, 'deathrow')
        self.assertFalse(build_b.requested_action)
        self.assertFalse(build_a_last.requested_action)
        self.assertFalse(children_b.requested_action)
