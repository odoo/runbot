# -*- coding: utf-8 -*-
import json
import subprocess
from unittest.mock import patch, mock_open
from odoo import Command, fields
from odoo.tests import Like
from odoo.tools import mute_logger
from odoo.exceptions import UserError
from odoo.addons.runbot.common import RunbotException
from .common import RunbotCase
from ..common import markdown_unescape


class TestBuildConfigStepCommon(RunbotCase):
    def setUp(self):
        super().setUp()

        self.Build = self.env['runbot.build']
        self.ConfigStep = self.env['runbot.build.config.step']
        self.Config = self.env['runbot.build.config']

        self.server_commit = self.Commit.create({
            'name': 'dfdfcfcf',
            'tree_hash': '0dfdfcfcf',
            'repo_id': self.repo_odoo.id,
        })
        self.parent_build = self.Build.create({
            'params_id': self.base_params.copy({'commit_link_ids': [(0, 0, {'commit_id': self.server_commit.id})]}).id,
            'local_result': 'ok',
        })
        self.start_patcher('find_patcher', 'odoo.addons.runbot.common.find', 0)


class TestCodeowner(TestBuildConfigStepCommon):
    def setUp(self):
        super().setUp()
        self.config_step = self.ConfigStep.create({
            'name': 'test_codeowner',
            'job_type': 'codeowner',
            'fallback_reviewer': 'codeowner-team',
        })
        self.child_config = self.Config.create({'name': 'test_config'})
        self.config_step.create_config_ids = [self.child_config.id]
        self.team1 = self.env['runbot.team'].create({'name': "Team1", 'github_team': "team_01"})
        self.team2 = self.env['runbot.team'].create({'name': "Team2", 'github_team': "team_02"})
        self.env['runbot.codeowner'].create({'github_teams': 'team_py', 'project_id': self.project.id, 'regex': '.*.py'})
        self.env['runbot.codeowner'].create({'github_teams': 'team_js', 'project_id': self.project.id, 'regex': '.*.js'})
        self.server_commit.name = 'dfdfcfcf'

    def test_codeowner_is_base(self):
        self.dev_bundle.is_base = True
        self.config_step._run_codeowner(self.parent_build)
        self.assertEqual(self.parent_build.log_ids.mapped('message'), [
            'Skipping base bundle',
        ])
        self.assertEqual(self.parent_build.local_result, 'ok')

    def test_codeowner_check_limits(self):
        self.parent_build.params_id.commit_link_ids[0].file_changed = 451
        self.parent_build.params_id.commit_link_ids[0].base_ahead = 51
        self.config_step._run_codeowner(self.parent_build)
        self.assertEqual(self.parent_build.log_ids.mapped('message'), [
            'Limit reached: dfdfcfcf has more than 50 commit (51) and will be skipped. Contact runbot team to increase your limit if it was intended',
            'Limit reached: dfdfcfcf has more than 450 modified files (451) and will be skipped. Contact runbot team to increase your limit if it was intended',
        ])
        self.assertEqual(self.parent_build.local_result, 'ko')

    def test_codeowner_draft(self):
        self.dev_pr.draft = True
        self.config_step._run_codeowner(self.parent_build)
        self.assertEqual(self.parent_build.log_ids.mapped('message'), [
            'Some pr are draft, skipping: 1234'
        ])
        self.assertEqual(self.parent_build.local_result, 'warn')

    def test_codeowner_draft_closed(self):
        self.dev_pr.draft = True
        self.dev_pr.alive = False
        self.assertEqual(self.parent_build.local_result, 'ok')

    def test_codeowner_forwardpot(self):
        self.dev_pr.pr_author = 'fw-bot'
        self.config_step._run_codeowner(self.parent_build)
        self.assertEqual(self.parent_build.log_ids.mapped('message'), [
            'Ignoring forward port pull request: 1234'
        ])
        self.assertEqual(self.parent_build.local_result, 'ok')

    def test_codeowner_invalid_target(self):
        self.dev_pr.target_branch_name = 'master-other-dev-branch'
        self.config_step._run_codeowner(self.parent_build)
        self.assertEqual(self.parent_build.log_ids.mapped('message'), [
            'Some pr have an invalid target: 1234'
        ])
        self.assertEqual(self.parent_build.local_result, 'ko')

    def test_codeowner_pr_duplicate(self):
        second_pr = self.Branch.create({
            'name': '1235',
            'is_pr': True,
            'remote_id': self.remote_odoo.id,
            'target_branch_name': self.dev_bundle.base_id.name,
            'pull_head_remote_id': self.remote_odoo.id,
            'pull_head_name': f'{self.remote_odoo.owner}:{self.dev_branch.name}',
        })
        self.assertEqual(second_pr.bundle_id.id, self.dev_bundle.id)
        self.config_step._run_codeowner(self.parent_build)
        self.assertEqual(self.parent_build.log_ids.mapped('message'), [
            "More than one open pr in this bundle for odoo: ['1234', '1235']"
        ])
        self.assertEqual(self.parent_build.local_result, 'ko')

    def test_get_module(self):
        self.assertEqual(self.repo_odoo.addons_paths, 'addons,core/addons')
        self.assertEqual('module1', self.repo_odoo._get_module('odoo/core/addons/module1/some/file.py'))
        self.assertEqual('module1', self.repo_odoo._get_module('odoo/addons/module1/some/file.py'))
        self.assertEqual('module_addons', self.repo_enterprise._get_module('enterprise/module_addons/some/file.py'))
        self.assertEqual(None, self.repo_odoo._get_module('odoo/core/module1/some/file.py'))
        self.assertEqual(None, self.repo_odoo._get_module('odoo/core/module/some/file.py'))

    def test_codeowner_regex_multiple(self):
        self.diff = 'addons/module/file.js\naddons/module/file.py\naddons/module/file.xml'
        self.config_step._run_codeowner(self.parent_build)
        messages = self.parent_build.log_ids.mapped('message')
        self.assertEqual(messages[1], 'Checking 2 codeowner regexed on 3 files')
        self.assertEqual(markdown_unescape(messages[2]), 'Adding team_js to reviewers for file [odoo/addons/module/file.js](https://False/blob/dfdfcfcf/addons/module/file.js)')
        self.assertEqual(markdown_unescape(messages[3]), 'Adding team_py to reviewers for file [odoo/addons/module/file.py](https://False/blob/dfdfcfcf/addons/module/file.py)')
        self.assertEqual(markdown_unescape(messages[4]), 'Adding codeowner-team to reviewers for file [odoo/addons/module/file.xml](https://False/blob/dfdfcfcf/addons/module/file.xml)')
        self.assertEqual(markdown_unescape(messages[5]), 'Requesting review for pull request [base/odoo:1234](https://example.com/base/odoo/pull/1234): codeowner-team, team_js, team_py')
        self.assertEqual(self.dev_pr.reviewers, 'codeowner-team,team_js,team_py')

    def test_codeowner_root_file(self):
        self.diff = 'addons/module/file.js\naddons/module/file.py\naddons/module/file.xml\ntest_file'
        self.config_step._run_codeowner(self.parent_build)
        messages = self.parent_build.log_ids.mapped('message')
        self.assertEqual(messages[1], 'Checking 2 codeowner regexed on 4 files')
        self.assertEqual(markdown_unescape(messages[2]), 'File odoo/test_file is at the root level and it looks like it could be a mistake, remove it or ensure that a codeowner rule is added for this file')
        self.assertEqual(markdown_unescape(messages[3]), 'Adding team_js to reviewers for file [odoo/addons/module/file.js](https://False/blob/dfdfcfcf/addons/module/file.js)')
        self.assertEqual(markdown_unescape(messages[4]), 'Adding team_py to reviewers for file [odoo/addons/module/file.py](https://False/blob/dfdfcfcf/addons/module/file.py)')
        self.assertEqual(markdown_unescape(messages[5]), 'Adding codeowner-team to reviewers for file [odoo/addons/module/file.xml](https://False/blob/dfdfcfcf/addons/module/file.xml)')
        self.assertEqual(markdown_unescape(messages[6]), 'No reviewer for file [odoo/test_file](https://False/blob/dfdfcfcf/test_file)')
        self.assertEqual(markdown_unescape(messages[7]), 'Requesting review for pull request [base/odoo:1234](https://example.com/base/odoo/pull/1234): codeowner-team, team_js, team_py')
        self.assertEqual(self.dev_pr.reviewers, 'codeowner-team,team_js,team_py')
        self.assertEqual(self.parent_build.local_result, 'ko')

    def test_codeowner_regex_some_already_on(self):
        self.diff = 'addons/module/file.js\naddons/module/file.py\naddons/module/file.xml'
        self.dev_pr.reviewers = 'codeowner-team,team_js'
        self.config_step._run_codeowner(self.parent_build)
        messages = self.parent_build.log_ids.mapped('message')
        self.assertEqual(markdown_unescape(messages[5]), 'Requesting review for pull request [base/odoo:1234](https://example.com/base/odoo/pull/1234): team_py')

    def test_codeowner_regex_all_already_on(self):
        self.diff = 'addons/module/file.js\naddons/module/file.py\naddons/module/file.xml'
        self.dev_pr.reviewers = 'codeowner-team,team_js,team_py'
        self.config_step._run_codeowner(self.parent_build)
        messages = self.parent_build.log_ids.mapped('message')
        self.assertEqual(messages[5], 'All reviewers are already on pull request [base/odoo:1234](https://example.com/base/odoo/pull/1234)')

    def test_codeowner_author_in_team(self):
        self.diff = 'addons/module/file.js\naddons/module/file.py\naddons/module/file.xml'
        self.team1.github_team = 'team_py'
        self.team1.github_logins = 'some_member,another_member'
        self.team1.skip_team_pr = True
        self.dev_pr.pr_author = 'some_member'
        self.config_step._run_codeowner(self.parent_build)
        messages = self.parent_build.log_ids.mapped('message')
        self.assertEqual(markdown_unescape(messages[5]), "Skipping teams ['team_py'] since author is part of the team members")
        self.assertEqual(markdown_unescape(messages[6]), 'Requesting review for pull request [base/odoo:1234](https://example.com/base/odoo/pull/1234): codeowner-team, team_js')
        self.assertEqual(self.dev_pr.reviewers, 'codeowner-team,team_js,team_py')

    def test_codeowner_ownership_base(self):
        module1 = self.env['runbot.module'].create({'name': "module1"})
        self.env['runbot.module.ownership'].create({'team_id': self.team1.id, 'module_id': module1.id})
        self.diff = '\n'.join([
            'core/addons/module1/some/file.py',
        ])
        self.config_step._run_codeowner(self.parent_build)
        messages = self.parent_build.log_ids.mapped('message')
        self.assertEqual(
            markdown_unescape(messages[2]), 
            'Adding team_01, team_py to reviewers for file [odoo/core/addons/module1/some/file.py](https://False/blob/dfdfcfcf/core/addons/module1/some/file.py)'
        )

    def test_codeowner_ownership_fallback(self):
        module1 = self.env['runbot.module'].create({'name': "module1"})
        self.env['runbot.module.ownership'].create({'team_id': self.team1.id, 'module_id': module1.id, 'is_fallback': True})
        self.diff = '\n'.join([
            'core/addons/module1/some/file.py',
        ])
        self.config_step._run_codeowner(self.parent_build)
        messages = self.parent_build.log_ids.mapped('message')
        self.assertEqual(
            markdown_unescape(messages[2]), 
            'Adding team_py to reviewers for file [odoo/core/addons/module1/some/file.py](https://False/blob/dfdfcfcf/core/addons/module1/some/file.py)'
        )

    def test_codeowner_ownership(self):
        module1 = self.env['runbot.module'].create({'name': "module1"})
        module2 = self.env['runbot.module'].create({'name': "module2"})
        self.env['runbot.module.ownership'].create({'team_id': self.team1.id, 'module_id': module1.id})
        self.env['runbot.module.ownership'].create({'team_id': self.team2.id, 'module_id': module2.id})
        self.diff = '\n'.join([
            'core/addons/module1/some/file.py',
            'core/addons/module2/some/file.ext',
            'core/addons/module3/some/file.js',
            'core/addons/module4/some/file.txt',
        ])
        self.config_step._run_codeowner(self.parent_build)
        messages = [markdown_unescape(message) for message in self.parent_build.log_ids.mapped('message')]
        self.assertEqual(messages, [
            'PR [base/odoo:1234](https://example.com/base/odoo/pull/1234) found for repo **odoo**',
            'Checking 2 codeowner regexed on 4 files',
            'Adding team_01, team_py to reviewers for file [odoo/core/addons/module1/some/file.py](https://False/blob/dfdfcfcf/core/addons/module1/some/file.py)',
            'Adding team_02 to reviewers for file [odoo/core/addons/module2/some/file.ext](https://False/blob/dfdfcfcf/core/addons/module2/some/file.ext)',
            'Adding team_js to reviewers for file [odoo/core/addons/module3/some/file.js](https://False/blob/dfdfcfcf/core/addons/module3/some/file.js)',
            'Adding codeowner-team to reviewers for file [odoo/core/addons/module4/some/file.txt](https://False/blob/dfdfcfcf/core/addons/module4/some/file.txt)',
            'Requesting review for pull request [base/odoo:1234](https://example.com/base/odoo/pull/1234): codeowner-team, team_01, team_02, team_js, team_py'
        ])

    def test_codeowner___init__log(self):
        module1 = self.env['runbot.module'].create({'name': "module1"})
        self.env['runbot.module.ownership'].create({'team_id': self.team1.id, 'module_id': module1.id})
        self.diff = '\n'.join([
            'core/addons/module1/some/__init__.py',
        ])
        self.config_step._run_codeowner(self.parent_build)
        logs = self.parent_build.log_ids

        self.assertEqual(
            logs[2]._markdown(),
            'Adding team_01, team_py to reviewers for file <a href="https://False/blob/dfdfcfcf/core/addons/module1/some/__init__.py">odoo/core/addons/module1/some/__init__.py</a>',
            '__init__.py should not be replaced by <ins>init</ins>.py'
        )

class TestBuildConfigStepRestore(TestBuildConfigStepCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.restore_config_step = cls.env['runbot.build.config.step'].create({
            'name': 'restore',
            'job_type': 'restore',
        })
        cls.restore_config = cls.env['runbot.build.config'].create({
            'name': 'Restore',
            'step_order_ids': [
                (0, 0, {'sequence': 10, 'step_id': cls.restore_config_step.id}),
            ],
        })

    def test_restore(self):
        # setup master branch
        master_batch = self.master_bundle._force()
        with mute_logger('odoo.addons.runbot.models.batch'):
            master_batch._process()
        reference_slot = master_batch.slot_ids
        trigger = reference_slot.trigger_id
        self.assertEqual(trigger.name, 'Server trigger', 'Just checking that we have a single slot')
        reference_build = reference_slot.build_id
        self.env['runbot.database'].create({
            'build_id': reference_build.id,
            'name': f'{reference_build.dest}-suffix',
        })
        reference_build.local_state = 'done'
        reference_build.local_result = 'ok'

        # custom trigger
        config_data = {
                'dump_trigger_id': trigger.id,
                'dump_suffix': 'suffix',
            }
        self.env['runbot.bundle.trigger.custom'].create({
            'bundle_id': self.dev_bundle.id,
            'config_id': self.restore_config.id,
            'trigger_id': trigger.id,
            'config_data': config_data,
        })

        # create dev build
        dev_batch = self.dev_bundle._force()
        with mute_logger('odoo.addons.runbot.models.batch'):
            dev_batch._process()
        dev_batch.base_reference_batch_id = master_batch  # not tested, this is not the purpose of this test
        dev_build = dev_batch.slot_ids.build_id
        self.assertEqual(dev_build.params_id.config_data, config_data)

        docker_params = self.restore_config_step._run_restore(dev_build)
        cmds = docker_params['cmd'].split(' && ')
        self.assertEqual(f'wget --retry-on-host-error https://False/runbot/static/build/{reference_build.dest}/logs/{reference_build.dest}-suffix.zip', cmds[2])
        self.assertEqual(f'psql -q {dev_build.dest}-suffix < dump.sql', cmds[8])
        self.called = True

class TestBuildConfigStepCreate(TestBuildConfigStepCommon):

    def setUp(self):
        super().setUp()
        self.config_step = self.ConfigStep.create({
            'name': 'test_step',
            'job_type': 'create_build',
            'number_builds': 2,
        })
        self.child_config = self.Config.create({'name': 'test_config'})
        self.config_step.create_config_ids = [self.child_config.id]

    def test_config_step_create_results(self):
        """ Test child builds are taken into account"""

        self.config_step._run_create_build(self.parent_build)
        self.assertEqual(len(self.parent_build.children_ids), 2, 'Two sub-builds should have been generated')

        # check that the result will be ignored by parent build
        for child_build in self.parent_build.children_ids:
            self.assertFalse(child_build.orphan_result)
            child_build.local_result = 'ko'
            self.assertEqual(child_build.global_result, 'ko')


        self.assertEqual(self.parent_build.global_result, 'ko')

    def test_config_step_create(self):
        """ Test the config step of type create """
        self.config_step.make_orphan = True
        self.config_step._run_create_build(self.parent_build)
        self.assertEqual(len(self.parent_build.children_ids), 2, 'Two sub-builds should have been generated')

        # check that the result will be ignored by parent build
        for child_build in self.parent_build.children_ids:
            self.assertTrue(child_build.orphan_result, 'An orphan result config step should mark the build as orphan_result')
            child_build.local_result = 'ko'
            # child_build._update_:globals()

        self.assertEqual(self.parent_build.global_result, 'ok')

    def test_config_step_create_child_data(self):
        """ Test the config step of type create """
        self.config_step.number_builds = 5
        json_config = {'child_data': [{'extra_params': '-i m1'}, {'extra_params': '-i m2'}]}
        self.parent_build = self.Build.create({
            'params_id': self.base_params.create({
                'version_id': self.version_13.id,
                'project_id': self.project.id,
                'config_id': self.default_config.id,
                'config_data': json_config,
            }).id,
        })

        self.config_step._run_create_build(self.parent_build)
        self.assertEqual(len(self.parent_build.children_ids), 10, '10 build should have been generated')

        # check that the result will be ignored by parent build
        for child_build in self.parent_build.children_ids:
            self.assertTrue(child_build.config_id, self.child_config)

    def test_config_step_create_child_data_unique(self):
        """ Test the config step of type create """
        json_config = {'child_data': {'extra_params': '-i m1'}, 'number_build': 5}
        self.parent_build = self.Build.create({
            'params_id': self.base_params.create({
                'version_id': self.version_13.id,
                'project_id': self.project.id,
                'config_id': self.default_config.id,
                'config_data': json_config,
            }).id,
        })

        self.config_step._run_create_build(self.parent_build)
        self.assertEqual(len(self.parent_build.children_ids), 5, '5 build should have been generated')

        # check that the result will be ignored by parent build
        for child_build in self.parent_build.children_ids:
            self.assertTrue(child_build.config_id, self.child_config)

    def test_config_step_create_child_data_with_config(self):
        """ Test the config step of type create """

        test_config_1 = self.Config.create({'name': 'test_config1'})
        test_config_2 = self.Config.create({'name': 'test_config2'})

        self.config_step.number_builds = 5
        json_config = {'child_data': [{'extra_params': '-i m1', 'config_id': test_config_1.id}, {'config_id': test_config_2.id}]}
        self.parent_build = self.Build.create({
            'params_id': self.base_params.create({
                'version_id': self.version_13.id,
                'project_id': self.project.id,
                'config_id': self.default_config.id,
                'config_data': json_config,
            }).id,
        })

        self.config_step._run_create_build(self.parent_build)
        self.assertEqual(len(self.parent_build.children_ids), 10, '10 build should have been generated')
        self.assertEqual(len(self.parent_build.children_ids.filtered(lambda b: b.config_id == test_config_1)), 5)
        self.assertEqual(len(self.parent_build.children_ids.filtered(lambda b: b.config_id == test_config_2)), 5)


class TestBuildConfigStepDynamic(TestBuildConfigStepCommon):

    def setUp(self):
        super().setUp()
        self.config_step = self.ConfigStep.create({
            'name': 'test_step',
            'job_type': 'dynamic',
            'number_builds': 2,
        })
        self.commit_server = self.Commit.create({
            'name': 'dfdfcfcf0000ffffffffffffffffffffffffffff',
            'tree_hash': '0dfdfcfcf0000fffffffffffffffffffffffffff',
            'repo_id': self.repo_odoo.id,
        })
        self.commit_addons = self.Commit.create({
            'name': 'dfdfcfcf0011ffffffffffffffffffffffffffff',
            'tree_hash': '0dfdfcfcf0011fffffffffffffffffffffffffff',
            'repo_id': self.repo_enterprise.id,
        })

        with open(__file__[:-25] + 'test_build_config_step_dynamic.json') as f:
            self.config_file = f.read()

        with open(__file__[:-25] + 'test_build_config_step_dynamic_extension.json') as f:
            self.config_file_extension = f.read()

        with open(__file__[:-25] + 'test_build_config_step_dynamic_l10n.json') as f:
            self.l10n_standalone_testing_file = f.read()

        self.config = self.Config.create({
            'name': 'Dynamic parallel testing',
            'step_order_ids': [
                (0, 0, {'sequence': 10, 'step_id': self.config_step.id}),
            ],
            'default_dynamic_config': self.config_file,
            'dynamic_config_extension': self.config_file_extension,
        })

        self.build = self.Build.create({
            'params_id': self.base_params.copy({
                'config_id': self.config.id,
                'commit_link_ids': [(0, 0, {'commit_id': self.commit_server.id}), (0, 0, {'commit_id': self.commit_addons.id})],
                }).id,
            'local_result': 'ok',
        })
        self.module_dependencies = {
            "test_mail": ["mail"],
            "mail": ["web"],
            "account": ["web"],
            "crm": ["web"],
            "project": ["web"],
            "test_l10n": ["l10n_be", "l10n_in"],
            "l10n_be": ["account"],
            "l10n_in": ["account"],
            "web_enterprise": ["web"],
        }

    def mock_git_helper(self, repo, cmd, input_data=None, raw=False):
        def make_catfile_output(commit, content):
            content_bytes = content.encode('utf-8')
            header = f"{commit} blob {len(content_bytes)}\n".encode()
            result = header + content_bytes + b"\n"
            return result

        if cmd == ['cat-file', '--batch']:
            if repo == self.repo_odoo and input_data == 'dfdfcfcf0000ffffffffffffffffffffffffffff:odoo/tests/.runbot/parallel_testing.json\n':
                return make_catfile_output('dfdfcfcf0000ffffffffffffffffffffffffffff', self.config_file)
            if repo == self.repo_odoo and input_data == 'dfdfcfcf0000ffffffffffffffffffffffffffff:odoo/tests/.runbot/l10n_standalone_testing.json\n':
                return make_catfile_output('dfdfcfcf0000ffffffffffffffffffffffffffff', self.l10n_standalone_testing_file)

            if "__manifest__.py" in input_data:
                modules_info = [
                    (line, line.split(':')[-1].split('/')[-2])
                    for line in input_data.splitlines()
                    if line.endswith('__manifest__.py')
                ]
                result = b""
                for original_query, module in modules_info:
                    content = '''{'name': '%s', 'depends': %s}''' % (module, self.module_dependencies.get(module, []))
                    result += make_catfile_output(original_query.split(':')[0], content)
                return result

        if cmd == ['cat-file', '--batch']:
            raise subprocess.CalledProcessError(cmd, 128)
        elif 'diff' in cmd:
            return 'odoo/addons/crm/some/file.py\nodoo/addons/project/some/file.py'
        return super().mock_git_helper(repo, cmd, input_data, raw)

    def test_module_filters(self):
        self.assertEqual(self.build._get_modules_to_test('-> !mail'), ['account', 'base', 'crm', 'documents'])
        self.assertEqual(self.build._get_modules_to_test('mail -> !web'), ['mail', 'project', 'test_l10n', 'test_lint', 'test_mail'])
        self.assertEqual(self.build._get_modules_to_test('web -> web'), ['web'])
        self.assertEqual(self.build._get_modules_to_test('!web ->'), ['web_enterprise'])
        self.assertEqual(self.build._get_modules_to_test('-> !mail, -crm'), ['account', 'base', 'documents'])
        self.assertEqual(self.build._get_modules_to_test('mail -> !web, !project'), ['mail', 'test_l10n', 'test_lint', 'test_mail'])
        self.assertEqual(self.build._get_modules_to_test('-*,odoo/*'), ['account', 'base', 'crm', 'hw_drivers', 'mail', 'project', 'test_l10n', 'test_lint', 'test_mail', 'web'])
        self.assertEqual(self.build._get_modules_to_test('-*,odoo/test_*'), ['test_l10n', 'test_lint', 'test_mail'])
        self.assertEqual(self.build._get_modules_to_test('-*,enterprise/*'), ['documents', 'l10n_be', 'l10n_in', 'web_enterprise'])
        self.assertEqual(self.build._get_modules_to_test('-*,web*'), ['web', 'web_enterprise'])
        self.assertEqual(self.build._get_modules_to_test('-*,web*,-enterprise/web*'), ['web'])

    def test_config_extension(self):
        self.assertEqual(self.build.dynamic_config['steps'][1]['cpu_limit'], 6500)
        self.assertEqual(json.loads(self.config.default_dynamic_config)['vars']['module_filter'], '*,-hw_*')
        self.assertEqual(self.build.dynamic_config['vars']['module_filter'], '*,-hw_*,-l10n_*')

    def test_parse_dynamic_entry(self):
        Step = self.env['runbot.build.config.step']

        def check_parse(entry, expected):
            res = Step._parse_dynamic_entry(entry, self.build, {'key': 'value', 'test_method': '.test_method'})
            self.assertEqual(res, expected)
        check_parse('{{-test_*|filter_all_modules}}', 'account,base,crm,documents,hw_drivers,l10n_be,l10n_in,mail,project,web,web_enterprise')
        check_parse('{{-*,web*|filter_all_modules}}', 'web,web_enterprise')
        check_parse('{{-*,web*|filter_all_modules|make_module_test_tags}}', '/web,/web_enterprise')
        check_parse('{{-*,web*|filter_all_modules|make_module_test_tags|prepend("some_tag")}}', 'some_tag/web,some_tag/web_enterprise')
        check_parse('{{-*,web*|filter_all_modules|make_module_test_tags|prepend(key)}}', 'value/web,value/web_enterprise')
        check_parse('{{-*,web*|filter_all_modules|make_module_test_tags|append(".test_method")}}', '/web.test_method,/web_enterprise.test_method')
        check_parse('{{-*,web*|filter_all_modules|make_module_test_tags|append(test_method)}}', '/web.test_method,/web_enterprise.test_method')

        self.patch(type(self.build), '_modified_modules', lambda cl, defaults=None: {'crm'})

        check_parse('{{*|filter_all_modules|modified_modules}}', 'crm')

    def test_modules_dependencies(self):
        self.assertEqual(self.build._get_modules_dependencies(['test_mail'], 1), ['mail', 'test_mail'])
        self.assertEqual(self.build._get_modules_dependencies(['test_mail']), ['base', 'mail', 'test_mail', 'web'])
        self.assertEqual(self.build._get_modules_dependencies(['test_l10n']), ['account', 'base', 'l10n_be', 'l10n_in', 'test_l10n', 'web'])
        self.assertEqual(self.build._get_modules_dependencies(['test_mail', 'test_l10n']), ['account', 'base', 'l10n_be', 'l10n_in', 'mail', 'test_l10n', 'test_mail', 'web'])
        self.assertEqual(self.build._get_modules_dependencies(['test_mail', 'test_l10n'], 1), ['l10n_be', 'l10n_in', 'mail', 'test_l10n', 'test_mail'])

        self.assertEqual(self.build._get_dependant_modules(['account'], 1), ['account', 'l10n_be', 'l10n_in'])
        self.assertEqual(self.build._get_dependant_modules(['account']), ['account', 'l10n_be', 'l10n_in', 'test_l10n'])
        self.assertEqual(self.build._get_dependant_modules(['base']), ['account', 'base', 'crm', 'documents', 'hw_drivers', 'l10n_be', 'l10n_in', 'mail', 'project', 'test_l10n', 'test_lint', 'test_mail', 'web', 'web_enterprise'])

    def check_server_cmd(self, cmd, install, test_enable, test_tags, db=None):
        self.assertIn('odoo/server.py', cmd)
        if install:
            self.assertIn('-i', cmd)
            cmd_install = cmd[cmd.index('-i') + 1].split(',')
            self.assertEqual(cmd_install, install)
        else:
            self.assertNotIn('-i', cmd)
        if test_enable:
            self.assertIn('--test-enable', cmd)
        else:
            self.assertNotIn('--test-enable', cmd)
        if test_tags:
            self.assertIn('--test-tags', cmd)
            cmd_test_tags = cmd[cmd.index('--test-tags') + 1]
            self.assertEqual(cmd_test_tags, test_tags)
        else:
            self.assertNotIn('--test-tags', cmd)

        if db:
            self.assertIn('-d', cmd)
            cmd_db = cmd[cmd.index('-d') + 1]
            self.assertEqual(cmd_db, db)

    def test_dynamic_step_parallel_testing(self):
        config = self.Config.create({
            'name': 'Dynamic parallel testing',
            'step_order_ids': [
                (0, 0, {'sequence': 10, 'step_id': self.config_step.id}),
            ],
            'dynamic_config_file_path': 'odoo/tests/.runbot/parallel_testing.json',
            'dynamic_config_extension': self.config_file_extension,
        })
        build = self.Build.create({
            'params_id': self.base_params.copy({
                'config_id': config.id,
                'commit_link_ids': [(0, 0, {'commit_id': self.commit_server.id}), (0, 0, {'commit_id': self.commit_addons.id})],
                }).id,
            'local_result': 'ok',
        })
        self.maxDiff = None

        self.start_patcher('_make_results', 'odoo.addons.runbot.models.build_config.ConfigStep._make_results', None)

        # 0.1. create at install builds
        build._schedule()
        self.assertEqual(build.active_step.id, self.config_step.id)
        self.assertEqual(build.dynamic_active_step_index, 0)
        self.assertEqual(len(build.children_ids), 2, 'Two sub-builds should have been generated')
        self.assertEqual(build.children_ids[0].config_id.id, config.id)
        self.assertEqual(build.children_ids[1].config_id.id, config.id)
        step_logs = build.log_ids[-3:]
        self.assertEqual(step_logs[0].message, 'Starting step **create_at_install** from config **Dynamic parallel testing**')
        self.assertEqual(step_logs[1].message, 'created with config Test at install')
        self.assertEqual(step_logs[2].message, 'created with config Test pylint')

        # 0.2. install test database
        self.assertFalse(self.docker_run_calls, "No docker run should have been called yet")
        build._schedule()()
        self.assertEqual(build.active_step.id, self.config_step.id)
        self.assertEqual(build.dynamic_active_step_index, 1)

        step_logs = build.log_ids[-2:]
        self.assertEqual(step_logs[0].message, Like('Starting step **install all** from config **Dynamic parallel testing**...'))
        self.assertEqual(step_logs[1].message, 'Using Dockerfile Tag [odoo:DockerDefault](/runbot/dockerfile_result/odoo:DockerDefault/None)')

        self.assertEqual(len(self.docker_run_calls), 1, "One docker run should have been called for install_all step")
        cmd = self.docker_run_calls[0][0]
        odoo_cmd = cmd.cmd
        self.check_server_cmd(odoo_cmd,
            install=['account', 'base', 'crm', 'documents', 'mail', 'project', 'test_l10n', 'test_lint', 'test_mail', 'web', 'web_enterprise'],
            test_enable=False,
            test_tags=None,
            db=f'{build.dest}-all',
        )
        # 0.3. create post install builds
        build._schedule()
        self.assertEqual(build.active_step.id, self.config_step.id)
        self.assertEqual(build.dynamic_active_step_index, 2)
        step_logs = build.log_ids[-6:]
        #self.assertEqual(step_logs[0].message, Like(f'Step install_all finished in ...{build.dest}-all.zip...'))
        self.assertEqual(step_logs[1].message, 'Starting step **create_post_install** from config **Dynamic parallel testing**')
        self.assertEqual(step_logs[2].message, 'created with config Test Post Install')
        self.assertEqual(step_logs[3].message, 'created with config Test Post Install')
        self.assertEqual(step_logs[4].message, 'created with config Test Post Install')
        self.assertEqual(step_logs[5].message, 'created with config Test Post Install')

        # 0.4. parent done
        build._schedule()
        self.assertEqual(build.active_step.id, False)
        self.assertEqual(build.dynamic_active_step_index, 3)
        self.assertEqual(build.local_state, 'done')

        ### Check children

        at_install, test_lint, post_install_1, post_install_2, post_install_3, post_install_4 = build.children_ids.sorted('id')

        # 2.1 at install builds
        self.docker_run_calls = []

        at_install._schedule()()

        cmd = self.docker_run_calls[0][0]
        odoo_cmd = cmd.cmd
        self.check_server_cmd(odoo_cmd,
            install=['account', 'base', 'crm', 'documents', 'mail', 'project', 'test_l10n', 'test_lint', 'test_mail', 'web', 'web_enterprise'],
            test_enable=True,
            test_tags='-post_install,-/test_lint',
        )

        # 3.1 at install builds
        self.docker_run_calls = []
        test_lint._schedule()()
        cmd = self.docker_run_calls[0][0]
        odoo_cmd = cmd.cmd
        self.check_server_cmd(odoo_cmd,
            install=['test_lint'],
            test_enable=True,
            test_tags='-post_install,/test_lint',
        )

        for post_install, expected_tags in [
            (post_install_1, '-at_install,/account,/base,/crm,/documents,/hw_drivers,/l10n_be,/l10n_in'),  # we need the blacklisted modules here
            (post_install_2, '-at_install,/mail,/project,/test_l10n,/test_lint,/test_mail'),
            (post_install_3, '-at_install,/web'),
            (post_install_4, '-at_install,/web_enterprise'),
        ]:
            with self.subTest(post_install=expected_tags):
                # 4.1 post install restore
                self.docker_run_calls = []
                post_install._schedule()()
                self.assertEqual(len(self.docker_run_calls), 1, "One docker run should have been called for post_install restore step")
                cmd = self.docker_run_calls[0][0]
                self.assertIn(f'{build.dest}/logs/{build.dest}-all.zip', cmd, 'The database from the parent should be downloaded by default')

                # 4.2 post install test
                post_install._schedule()()
                self.assertEqual(len(self.docker_run_calls), 2, "Two docker run should have been called for post_install restore and post install step")
                self.check_server_cmd(self.docker_run_calls[1][0].cmd,
                    install=None,
                    test_enable=True,
                    test_tags=expected_tags,
                )
                test_module_filter = post_install.params_id.config_data['dynamic_vars']['test_module_filter']
                self.assertIn('->', test_module_filter)
                self.assertEqual(post_install.description, f'Post install tests for **{test_module_filter}**')

    def test_dynamic_step_l10n_standalone(self):
        self.addons_per_repo[self.repo_enterprise] += [
            ('', 'l10n_edi_be', '__manifest__.py'),
            ('', 'l10n_edi_in', '__manifest__.py'),
            ('', 'l10n_reports_be', '__manifest__.py'),
            ('', 'l10n_reports_in', '__manifest__.py'),
            ('', 'l10n_hr_payroll_be', '__manifest__.py'),
            ('', 'l10n_hr_payroll_in', '__manifest__.py'),
        ]
        config = self.Config.create({
            'name': 'Dynamic L10N Standalone Testing',
            'step_order_ids': [
                (0, 0, {'sequence': 10, 'step_id': self.config_step.id}),
            ],
            'dynamic_config_file_path': 'odoo/tests/.runbot/l10n_standalone_testing.json',
        })
        build = self.Build.create({
            'params_id': self.base_params.copy({
                'config_id': config.id,
                'commit_link_ids': [(0, 0, {'commit_id': self.commit_server.id}), (0, 0, {'commit_id': self.commit_addons.id})],
                }).id,
            'local_result': 'ok',
        })
        self.maxDiff = None

        self.start_patcher('_make_results', 'odoo.addons.runbot.models.build_config.ConfigStep._make_results', None)

        # 0.1. install test_l10 database
        self.assertFalse(self.docker_run_calls, "No docker run should have been called yet")
        build._schedule()()
        self.assertEqual(build.active_step.id, self.config_step.id)
        self.assertEqual(build.dynamic_active_step_index, 0)

        step_logs = build.log_ids[-2:]
        self.assertEqual(step_logs[0].message, Like('Starting step **Install test_l10n database** from config...'))
        self.assertEqual(step_logs[1].message, 'Using Dockerfile Tag [odoo:DockerDefault](/runbot/dockerfile_result/odoo:DockerDefault/None)')

        self.assertEqual(len(self.docker_run_calls), 1, "One docker run should have been called for install_all step")
        cmd = self.docker_run_calls[0][0]
        odoo_cmd = cmd.cmd
        self.check_server_cmd(odoo_cmd,
            install=['test_l10n'],
            test_enable=False,
            test_tags=None,
            db=f'{build.dest}-l10n',
        )

        # 0.2 run standalone l10n script
        self.docker_run_calls = []
        build._schedule()()
        self.assertEqual(len(self.docker_run_calls), 1, "One docker run should have been called for install_all step")
        cmd = self.docker_run_calls[0][0]
        self.assertEqual(cmd.build(), f'odoo/odoo/tests/test_module_operations.py -d {build.dest}-l10n --data-dir /data/build/datadir/ --addons-path odoo/addons,odoo/core/addons,enterprise --standalone all_l10n')

        # 0.3. create post install builds
        build._schedule()
        self.assertEqual(build.active_step.id, self.config_step.id)
        self.assertEqual(build.dynamic_active_step_index, 2)
        step_logs = build.log_ids[-6:]
        #self.assertEqual(step_logs[0].message, Like(f'Step install_all finished in ...{build.dest}-all.zip...'))
        self.assertEqual(step_logs[1].message, 'Starting step **Create post install** from config **Dynamic L10N Standalone Testing**')
        self.assertEqual(step_logs[2].message, 'created with config Test Post Install')
        self.assertEqual(step_logs[3].message, 'created with config Test Post Install')
        self.assertEqual(step_logs[4].message, 'created with config Test Post Install')
        self.assertEqual(step_logs[5].message, 'created with config Test Post Install')

        # 0.4. parent done
        build._schedule()
        self.assertEqual(build.active_step.id, False)
        self.assertEqual(build.dynamic_active_step_index, 3)
        self.assertEqual(build.local_state, 'done')

        ### Check children

        post_install_1, post_install_2, post_install_3, post_install_4 = build.children_ids.sorted('id')

        for post_install, expected_tags in [
            (post_install_1, '-external,-external_l10n,post_install_l10n/l10n_hr_payroll_be,post_install_l10n/l10n_hr_payroll_in'),  # we need the blacklisted modules here
            (post_install_2, '-external,-external_l10n,post_install_l10n/l10n_edi_be,post_install_l10n/l10n_edi_in'),
            (post_install_3, '-external,-external_l10n,post_install_l10n/l10n_reports_be,post_install_l10n/l10n_reports_in'),
            (post_install_4, Like('-external,-external_l10n,post_install_l10n/account,post_install_l10n/base,post_install_l10n/crm,...')),
        ]:
            with self.subTest(post_install=expected_tags):
                # 4.1 post install restore
                self.docker_run_calls = []
                post_install._schedule()()
                self.assertEqual(len(self.docker_run_calls), 1, "One docker run should have been called for post_install restore step")
                cmd = self.docker_run_calls[0][0]
                self.assertIn(f'{build.dest}/logs/{build.dest}-l10n.zip', cmd, 'The database from the parent should be downloaded by default')

                # 4.2 post install test
                post_install._schedule()()
                self.assertEqual(len(self.docker_run_calls), 2, "Two docker run should have been called for post_install restore and post install step")
                self.check_server_cmd(self.docker_run_calls[1][0].cmd,
                    install=None,
                    test_enable=True,
                    test_tags=expected_tags,
                )
                test_module_filter = post_install.params_id.config_data['dynamic_vars']['test_module_filter']
                self.assertEqual(post_install.description, f'Post install tests for **{test_module_filter}**')

    def test_foreach_module(self):
        dynamic_config = '''{
            "name": "Foreach module testing",
            "steps": [{
                "name": "Create module builds",
                "job_type": "create_build",
                "for_each_module": "{{-test_*|filter_default_modules}}",
                "children": [{
                    "name": "Test single module",
                    "description": "Post install tests for **{{module}}**",
                    "steps": [{
                        "name": "Start single module test",
                        "job_type": "odoo",
                        "install_modules": "{{module}}",
                        "test_tags": "{{module|make_module_test_tags}}"
                    }]
                }]
            }]
        }'''
        self.config.default_dynamic_config = dynamic_config
        self.config.step_ids[0]._run_dynamic(self.build)
        self.assertEqual(self.build.children_ids.mapped('description'),
            [
                'Post install tests for **account**',
                'Post install tests for **base**',
                'Post install tests for **crm**',
                'Post install tests for **documents**',
                'Post install tests for **mail**',
                'Post install tests for **project**',
                'Post install tests for **web**',
                'Post install tests for **web_enterprise**',
        ])

    def test_foreach_modified_module(self):
        dynamic_config = '''{
            "name": "Foreach module testing",
            "steps": [{
                "name": "Create module builds",
                "job_type": "create_build",
                "for_each_module": "{{-test_*|filter_default_modules|modified_modules}}",
                "children": [{
                    "name": "Test single module",
                    "description": "Post install tests for **{{module}}**",
                    "steps": [{
                        "name": "Start single module test",
                        "job_type": "odoo",
                        "install_modules": "{{module}}",
                        "test_tags": "{{module|make_module_test_tags}}"
                    }]
                }]
            }]
        }'''

        self.patch(type(self.build), '_modified_modules', lambda cl, defaults=None: {'crm'})
        self.config.default_dynamic_config = dynamic_config
        self.config.step_ids[0]._run_dynamic(self.build)
        self.assertEqual(self.build.children_ids.mapped('description'),
        [
            'Post install tests for **crm**',
        ])

    def test_modified_existing_module(self):
        dynamic_config = '''{
            "vars": {
                "modified_modules": "{{*|filter_all_modules|modified_modules}}",
                "test_modules": "{{modified_modules|prepend('test_')|select_existing_modules}}",
                "modules_to_test": "{{modified_modules|union(test_modules)}}"
            },
            "name": "Foreach module testing",
            "steps": [{
                "name": "Create module builds",
                "job_type": "create_build",
                "children": [{
                    "name": "Test single module",
                    "description": "Post install tests for **{{modules_to_test}}**",
                    "steps": [{
                        "name": "Start single module test",
                        "job_type": "odoo",
                        "install_modules": "{{modules_to_test}}",
                        "test_tags": "{{modules_to_test|make_module_test_tags}}"
                    }]
                }]
            }]
        }'''

        self.patch(type(self.build), '_modified_modules', lambda cl, defaults=None: {'crm', 'mail'})
        self.config.default_dynamic_config = dynamic_config
        self.config.step_ids[0]._run_dynamic(self.build)
        self.assertEqual(self.build.children_ids.mapped('description'),
        [
                'Post install tests for **crm,mail,test_mail**',
        ])
        child_dynamic_vars = self.build.children_ids.params_id.config_data['dynamic_vars']
        self.assertEqual(child_dynamic_vars, {
            'modified_modules': 'crm,mail',
            'test_modules': 'test_mail',
            'modules_to_test': 'crm,mail,test_mail',
        })

    def test_modified_existing_module_parallel(self):
        dynamic_config = '''{
            "vars": {
                "modified_modules": "{{*|filter_all_modules|modified_modules}}",
                "modules_to_test": "{{modified_modules|prepend('test_')|select_existing_modules|union(modified_modules)}}"
            },
            "name": "Parallel split modified",
            "steps": [{
                "name": "Create module builds",
                "job_type": "create_build",
                "for_each_vars": [{
                        "test_module_filter": "{{modules_to_test}},->!mail"
                    },
                    {
                        "test_module_filter": "{{modules_to_test}},mail->!website"
                    },
                    {
                        "test_module_filter": "{{modules_to_test}},website->"
                    }
                ],
                "if": "{{child_modules_to_test}}",
                "children": [{
                    "vars": {
                        "child_modules_to_test": "{{test_module_filter|select_existing_modules}}"
                    },
                    "name": "Test single module",
                    "description": "Post install tests for **{{child_modules_to_test}}**",
                    "steps": [{
                        "name": "Start single module test",
                        "job_type": "odoo",
                        "install_modules": "{{child_modules_to_test}}",
                        "test_tags": "{{child_modules_to_test|make_module_test_tags}}"
                    }]
                }]
            }]
        }'''

        self.patch(type(self.build), '_modified_modules', lambda cl, defaults=None: {'crm', 'mail'})
        self.config.default_dynamic_config = dynamic_config
        self.config.step_ids[0]._run_dynamic(self.build)
        self.assertEqual(self.build.children_ids.mapped('description'),
        [
                'Post install tests for **crm**',
                'Post install tests for **mail,test_mail**',
        ])

        self.assertEqual(self.build.children_ids[0].params_id.config_data['dynamic_vars']['child_modules_to_test'], 'crm')
        self.assertEqual(self.build.children_ids[1].params_id.config_data['dynamic_vars']['child_modules_to_test'], 'mail,test_mail')

    def test_modified_existing_module_parallel_relations(self):
        dynamic_config = '''{
            "vars": [
                {"module_filter": "*,-hw_*,-*l10n_*,-theme_*,-account_bacs,-account_reports_cash_basis,-auth_ldap,-base_gengo,-document_ftp,-iot_drivers,-note_pad,-odoo_referral,-odoo_referral_portal,-pad,-pad_project,-pos_blackbox_be,-pos_cache,-pos_six,-social_demo,-website_gengo,-website_instantclick,test_l10n_be_hr_payroll_account,test_l10n_us_hr_payroll_account"},
                {"_modified_modules": "{{module_filter|filter_all_modules|modified_modules}}"},
                {"_modules_dependencies": "{{_modified_modules|get_dependencies(1)}}"},
                {"_dependant_modules": "{{_modified_modules|get_dependant(1)}}"},
                {"_test_modules": "{{_modified_modules|prepend('test_')|select_existing_modules}}"},
                {"_modules_to_test": "{{_modified_modules|union(_test_modules)|union(_dependant_modules)|union(_modules_dependencies)}}"}
            ],
            "name": "Parallel split modified",
            "steps": [{
                "name": "Create module builds",
                "job_type": "create_build",
                "for_each_vars": [{
                        "_test_module_filter": "{{_modules_to_test}},->!mail"
                    },
                    {
                        "_test_module_filter": "{{_modules_to_test}},mail->!website"
                    },
                    {
                        "_test_module_filter": "{{_modules_to_test}},website->"
                    }
                ],
                "if": "{{child_modules_to_test}}",
                "log": "Modified modules: {{_modified_modules}}\\nDepenencies: {{_modules_dependencies}}\\nDependant: {{_dependant_modules}}\\nTest modules: {{_test_modules}}",
                "children": [{
                    "vars": {
                        "child_modules_to_test": "{{_test_module_filter|select_existing_modules}}"
                    },
                    "name": "Test single module",
                    "description": "Post install tests for **{{child_modules_to_test}}**",
                    "steps": [{
                        "name": "Start single module test",
                        "job_type": "odoo",
                        "install_modules": "{{child_modules_to_test}}",
                        "test_tags": "{{child_modules_to_test|make_module_test_tags}}"
                    }]
                }]
            }]
        }'''

        self.patch(type(self.build), '_modified_modules', lambda cl, defaults=None: {'crm', 'mail'})
        self.config.default_dynamic_config = dynamic_config
        self.config.step_ids[0]._run_dynamic(self.build)
        self.assertEqual(self.build.children_ids.mapped('description'),
        [
                'Post install tests for **crm**',
                'Post install tests for **mail,test_mail,web**',
        ])
        self.assertEqual(self.build.children_ids[0].params_id.config_data['dynamic_vars']['child_modules_to_test'], 'crm')
        self.assertEqual(self.build.children_ids[1].params_id.config_data['dynamic_vars']['child_modules_to_test'], 'mail,test_mail,web')
        self.assertEqual(list(self.build.children_ids[0].params_id.config_data['dynamic_vars'].keys()), ['module_filter', 'child_modules_to_test'])


class TestBuildConfigStep(TestBuildConfigStepCommon):

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
            'coverage': True,
        })

        cmd = config_step._run_install_odoo(self.parent_build)['cmd']
        self.assertEqual(cmd.cmd[:10], ['python3', '-m', 'coverage', 'run', '--branch', '--source', '/data/build', '--omit', '*__manifest__.py,odoo/addons/hw_drivers/*', 'odoo/server.py'])
        self.assertIn(['python3', '-m', 'coverage', 'html', '-d', '/data/build/coverage', '--ignore-errors'], cmd.finals)
        self.assertIn(['python3', '-m', 'coverage', 'xml', '-o', '/data/build/logs/coverage.xml', '--ignore-errors'], cmd.finals)


    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_dump(self, mock_checkout):
        config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'install_odoo',
        })

        dest = self.parent_build.dest

        cmd = config_step._run_install_odoo(self.parent_build)['cmd']
        self.assertEqual(cmd.cmd[:2], ['python3', 'odoo/server.py'])
        self.assertEqual(cmd.finals[0], ['pg_dump', '%s-all' % dest, '>', '/data/build/logs/%s-all//dump.sql' % dest])
        self.assertEqual(cmd.finals[1], ['cp', '-r', '/data/build/datadir/filestore/%s-all' % dest, '/data/build/logs/%s-all//filestore/' % dest])
        self.assertEqual(cmd.finals[2], ['cd', '/data/build/logs/%s-all/' % dest, '&&', 'zip', '-rmq9', '/data/build/logs/%s-all.zip' % dest, '*'])

    def get_test_tags(self, params):
        cmds = params['cmd'].build().split(' && ')
        self.assertEqual(cmds[1].split(' odoo/server.py')[0], 'python3')
        return cmds[1].split('--test-tags ')[1].split(' --')[0]

    def get_odoo_cmd(self, params):
        cmds = params['cmd'].build().split(' && ')
        self.assertTrue(any('odoo/server.py' in cmd for cmd in cmds), 'did not find start command')
        return next(iter(cmd for cmd in cmds if 'odoo/server.py' in cmd))

    @patch('odoo.addons.runbot.models.build.BuildResult._parse_config')
    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_install_tags(self, mock_checkout, parse_config):
        parse_config.return_value = {'--test-enable', '--test-tags'}
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
        params = config_step._run_install_odoo(self.parent_build)
        tags = self.get_test_tags(params)
        self.assertEqual(tags, '/module,:class.method')

        config_step.enable_auto_tags = True
        params = config_step._run_install_odoo(self.parent_build)
        tags = self.get_test_tags(params)
        self.assertEqual(tags, '/module,:class.method,-:otherclass.othertest')

    @patch('odoo.addons.runbot.models.build.BuildResult._parse_config')
    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_install_custom_tags(self, mock_checkout, parse_config):
        parse_config.return_value = {'--test-enable', '--test-tags'}
        config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'install_odoo',
            'enable_auto_tags': True,
        })
        self.env['runbot.build.error'].create({
            'content': 'foo',
            'random': True,
            'test_tags': ':otherclass.othertest'
        })

        child = self.parent_build._add_child({'config_data': {'test_tags': '-at_install,/module1,/module2'}})

        params = config_step._run_install_odoo(child)
        tags = self.get_test_tags(params)
        self.assertEqual(tags, '-at_install,/module1,/module2,-:otherclass.othertest')

    @patch('odoo.addons.runbot.models.build.BuildResult._parse_config')
    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_install_custom_env_variables(self, mock_checkout, parse_config):
        parse_config.return_value = {'--test-enable', '--test-tags'}
        config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'install_odoo'
        })

        child = self.parent_build._add_child({'config_data': {'env_variables': 'CHROME_CPU_THROTTLE=10'}})

        params = config_step._run_install_odoo(child)
        env_variables = params.get('env_variables', [])
        self.assertEqual(env_variables, ['CHROME_CPU_THROTTLE=10'])

    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_db_name(self, mock_checkout):
        config_step = self.ConfigStep.create({
            'name': 'default',
            'job_type': 'install_odoo',
            'custom_db_name': 'custom',
        })

        config_step._run_step(self.parent_build)()

        self.assertEqual(len(self.docker_run_calls), 1)
        for cmd, *_ in self.docker_run_calls:
            db_suffix = cmd.cmd[cmd.index('-d') + 1].split('-')[-1]
            self.assertEqual(db_suffix, 'custom')

        self.docker_run_calls = []

        parent_build_params = self.parent_build.params_id.copy({'config_data': {'db_name': 'custom_build'}})
        parent_build = self.parent_build.copy({'params_id': parent_build_params.id})
        config_step._run_step(parent_build)()

        config_step = self.ConfigStep.create({
            'name': 'run_test',
            'job_type': 'run_odoo',
            'custom_db_name': 'custom',
        })
        config_step._run_step(parent_build)()

        self.assertEqual(len(self.docker_run_calls), 2)
        for cmd, *_ in self.docker_run_calls:
            db_suffix = cmd.cmd[cmd.index('-d') + 1].split('-')[-1]
            self.assertEqual(db_suffix, 'custom_build')


    @patch('odoo.addons.runbot.models.build.BuildResult._parse_config')
    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_run_python(self, mock_checkout, parse_config):
        """minimal test for python steps. Also test that `-d` in cmd creates a database"""
        parse_config.return_value = {}
        test_code = """cmd = build._cmd()
cmd += ['-d', 'test_database']
docker_params = dict(cmd=cmd)
        """
        config_step = self.ConfigStep.create({
            'name': 'default',
            'job_type': 'python',
            'python_code': test_code,
        })

        config_step._run_step(self.parent_build)()

        self.assertEqual(self.docker_run_calls[0][0].build(), Like('python3 -m pip install ... && python3 odoo/server.py...-d test_database...'))
        db = self.env['runbot.database'].search([('name', '=', 'test_database')])
        self.assertEqual(db.build_id, self.parent_build)

    def test_run_python_run(self):
        """minimal test for python steps. Also test that `-d` in cmd creates a database"""
        test_code = """
def run():
    return {'a': 'b'}
"""
        config_step = self.ConfigStep.create({
            'name': 'default',
            'job_type': 'python',
            'python_code': test_code,
        })

        retult = config_step._run_python(self.parent_build)
        self.assertEqual(retult, {'a': 'b'})

    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_sub_command(self, mock_checkout):
        config_step = self.ConfigStep.create({
            'name': 'default',
            'job_type': 'install_odoo',
            'sub_command': 'subcommand',
        })
        config_step._run_step(self.parent_build)()
        self.assertEqual(len(self.docker_run_calls), 1)
        self.assertEqual(self.docker_run_calls[0][0].build(), Like('python3 -m pip install ... && python3 odoo/server.py subcommand ...'))


    @patch('odoo.addons.runbot.models.build.BuildResult._parse_config')
    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_install_demo_mode_default_default_without_demo(self, mock_checkout, parse_config):
        # Test demo_mode = 'default' when the default is without_demo
        parse_config.return_value = {'--with-demo'}
        config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'install_odoo',
        })
        child = self.parent_build._add_child({'config_data': {}})

        params = config_step._run_install_odoo(child)
        cmd = self.get_odoo_cmd(params)
        self.assertNotIn('--with-demo', cmd)
        self.assertNotIn('--without-demo', cmd)

        child.params_id.config_data = {'demo_mode': 'default'}
        params = config_step._run_install_odoo(child)
        cmd = self.get_odoo_cmd(params)
        self.assertNotIn('--with-demo', cmd)
        self.assertNotIn('--without-demo', cmd)

    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_install_demo_mode_default_default_with_demo(self, mock_checkout):
        # Test demo_mode = 'default' when the default is with_demo
        config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'install_odoo',
        })
        child = self.parent_build._add_child({'config_data': {}})

        params = config_step._run_install_odoo(child)
        cmd = self.get_odoo_cmd(params)
        self.assertNotIn('--with-demo', cmd)
        self.assertNotIn('--without-demo', cmd)

        child.params_id.config_data = {'demo_mode': 'default'}
        params = config_step._run_install_odoo(child)
        cmd = self.get_odoo_cmd(params)
        self.assertNotIn('--with-demo', cmd)
        self.assertNotIn('--without-demo', cmd)

    @patch('odoo.addons.runbot.models.build.BuildResult._parse_config')
    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_install_demo_mode_with_demo_default_without_demo(self, mock_checkout, parse_config):
        # Test demo_mode = 'with_demo' when the default is without_demo
        parse_config.return_value = {'--with-demo'}
        config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'install_odoo',
            'demo_mode': 'with_demo',
        })
        child = self.parent_build._add_child({'config_data': {}})

        params = config_step._run_install_odoo(child)
        cmd = self.get_odoo_cmd(params)
        self.assertIn('--with-demo', cmd)
        self.assertNotIn('--without-demo', cmd)

        config_step.demo_mode = 'default'
        child.params_id.config_data = {'demo_mode': 'with_demo'}
        params = config_step._run_install_odoo(child)
        cmd = self.get_odoo_cmd(params)
        self.assertIn('--with-demo', cmd)
        self.assertNotIn('--without-demo', cmd)

    @patch('odoo.addons.runbot.models.build.BuildResult._parse_config')
    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_install_demo_mode_with_demo_default_with_demo(self, mock_checkout, parse_config):
        # Test demo_mode = 'with_demo' when the default is with_demo
        parse_config.return_value = {}
        config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'install_odoo',
            'demo_mode': 'with_demo',
        })
        child = self.parent_build._add_child({'config_data': {}})

        params = config_step._run_install_odoo(child)
        cmd = self.get_odoo_cmd(params)
        self.assertNotIn('--with-demo', cmd)
        self.assertNotIn('--without-demo', cmd)

        config_step.demo_mode = 'default'
        child.params_id.config_data = {'demo_mode': 'with_demo'}
        params = config_step._run_install_odoo(child)
        cmd = self.get_odoo_cmd(params)
        self.assertNotIn('--with-demo', cmd)
        self.assertNotIn('--without-demo', cmd)

    @patch('odoo.addons.runbot.models.build.BuildResult._parse_config')
    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_install_demo_mode_without_demo_default_without_demo(self, mock_checkout, parse_config):
        # Test demo_mode = 'without_demo' when the default is without_demo
        parse_config.return_value = {'--with-demo'}
        config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'install_odoo',
            'demo_mode': 'without_demo',
        })
        child = self.parent_build._add_child({'config_data': {}})

        params = config_step._run_install_odoo(child)
        cmd = self.get_odoo_cmd(params)
        self.assertNotIn('--with-demo', cmd)
        self.assertNotIn('--without-demo', cmd)

        config_step.demo_mode = 'default'
        child.params_id.config_data = {'demo_mode': 'without_demo'}
        params = config_step._run_install_odoo(child)
        cmd = self.get_odoo_cmd(params)
        self.assertNotIn('--with-demo', cmd)
        self.assertNotIn('--without-demo', cmd)

    @patch('odoo.addons.runbot.models.build.BuildResult._parse_config')
    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_install_demo_mode_without_demo_default_with_demo(self, mock_checkout, parse_config):
        # Test demo_mode = 'without_demo' when the default is with_demo
        parse_config.return_value = {}
        config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'install_odoo',
            'demo_mode': 'without_demo',
        })
        child = self.parent_build._add_child({'config_data': {}})

        params = config_step._run_install_odoo(child)
        cmd = self.get_odoo_cmd(params)
        self.assertNotIn('--with-demo', cmd)
        self.assertIn('--without-demo', cmd)

        config_step.demo_mode = 'default'
        child.params_id.config_data = {'demo_mode': 'without_demo'}
        params = config_step._run_install_odoo(child)
        cmd = self.get_odoo_cmd(params)
        self.assertNotIn('--with-demo', cmd)
        self.assertIn('--without-demo', cmd)

    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_network_can_be_enable(self, mock_checkout):
        """ test that network can be disabled with config_data """
        config_step = self.ConfigStep.create({
            'name': 'default',
            'job_type': 'install_odoo',
        })

        # by default, network is disabled
        def first_docker_run(cmd, log_path, *args, **kwargs):
            self.assertFalse(kwargs['network_enabled'])

        self.docker_run_patch = first_docker_run
        config_step._run_step(self.parent_build)()

        def second_docker_run(cmd, log_path, *args, **kwargs):
            self.assertTrue(kwargs['network_enabled'])

        self.docker_run_patch = second_docker_run

        parent_build_params = self.parent_build.params_id.copy({'config_data': {'network_enabled': True}})
        parent_build = self.parent_build.copy({'params_id': parent_build_params.id})
        config_step._run_step(parent_build)()


    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_run_python_networkcan_be_enabled(self, mock_checkout):
        """test that docker network can be enabled from python step"""
        test_code = """cmd = build._cmd()
docker_params = dict(cmd=cmd, network_enabled=True)
        """
        config_step = self.ConfigStep.create({
            'name': 'default',
            'job_type': 'python',
            'python_code': test_code,
        })

        config_step._run_step(self.parent_build)()
        self.assertEqual(len(self.docker_run_calls), 1)
        self.assertTrue(self.docker_run_calls[0][3]['network_enabled'])

    @patch('odoo.addons.runbot.models.build.BuildResult._parse_config')
    @patch('odoo.addons.runbot.models.build.BuildResult._checkout')
    def test_install_custom_parametric_tags(self, mock_checkout, parse_config):
        parse_config.return_value = {'--test-enable', '--test-tags'}
        config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'install_odoo',
            'enable_auto_tags': True,
        })
        self.env['runbot.build.error'].create({
            'content': 'foo',
            'random': True,
            'test_tags': '/web/bar.py[@snafu/other test with spaces]'
        })

        self.assertIn('-/web/bar.py[@snafu/other test with spaces]', self.env['runbot.build.error']._disabling_tags(), 'Parametric disabling test-tag should be returned by _disabling_tags')

        child = self.parent_build._add_child({'config_data': {'test_tags': '-at_install, /web/foo.py:WebSuite.test_unit_desktop[@bar/test with spaces]'}})

        params = config_step._run_install_odoo(child)
        tags = self.get_test_tags(params)
        self.assertEqual(tags, '"-at_install,/web/foo.py:WebSuite.test_unit_desktop[@bar/test with spaces],-/web/bar.py[@snafu/other test with spaces]"')

class TestMakeResult(RunbotCase):

    def setUp(self):
        super(TestMakeResult, self).setUp()
        self.ConfigStep = self.env['runbot.build.config.step']
        self.Config = self.env['runbot.build.config']
        self.patchers['getmtime'].return_value = 7200
        self.logs = []
        def _log(build, func, message, level='INFO', log_type='runbot', path='runbot'):
            self.logs.append((level, message))

        self.start_patcher('log_patcher', 'odoo.addons.runbot.models.build.BuildResult._log', new=_log)

        self.build = self.Build.create({
            'params_id': self.base_params.id,
        })
        self.config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'install_odoo',
            'test_tags': '/module,:class.method',
        })

    def test_make_result_ok(self):
        file_content = """
Loading stuff
odoo.stuff.modules.loading: Modules loaded.
Some post install stuff
Initiating shutdown
"""
        with patch('builtins.open', mock_open(read_data=file_content)):
            self.config_step._make_results(self.build)
        self.assertEqual(str(self.build.job_end), '1970-01-01 02:00:00')
        self.assertEqual(self.logs, [('INFO', 'Getting results for build %s' % self.build.dest)])
        self.assertEqual(self.build.local_result, 'ok')

    def test_make_result_no_shutdown(self):
        file_content = """
Loading stuff
odoo.stuff.modules.loading: Modules loaded.
Some post install stuff
        """
        with patch('builtins.open', mock_open(read_data=file_content)):
            self.config_step._make_results(self.build)
        self.assertEqual(str(self.build.job_end), '1970-01-01 02:00:00')
        self.assertEqual(self.build.local_result, 'ko')
        self.assertEqual(self.logs, [
            ('INFO', 'Getting results for build %s' % self.build.dest),
            ('ERROR', 'No "Initiating shutdown" found in logs.\n'
   '\n'
   'Loading stuff\n'
   'odoo.stuff.modules.loading: Modules loaded.\n'
   'Some post install stuff\n'
   '        ')])

    def test_make_result_no_loaded(self):
        file_content = """
Loading stuff
"""
        with patch('builtins.open', mock_open(read_data=file_content)):
            self.config_step._make_results(self.build)
        self.assertEqual(str(self.build.job_end), '1970-01-01 02:00:00')
        self.assertEqual(self.build.local_result, 'ko')
        self.assertEqual(self.logs, [
            ('INFO', 'Getting results for build %s' % self.build.dest),
            ('ERROR', 'Modules loaded not found in logs\n\nLoading stuff\n'),
        ])

    traceback_example = """Traceback (most recent call last):
  File "/data/build/odoo/odoo-bin", line 5, in <module>
    import odoo
  File "/data/build/odoo/odoo/__init__.py", line 134, in <module>
    from . import modules
  File "/data/build/odoo/odoo/modules/__init__.py", line 8, in <module>
    from . import db, graph, loading, migration, module, registry, neutralize
  File "/data/build/odoo/odoo/modules/graph.py", line 11, in <module>
    import odoo.tools as tools
  File "/data/build/odoo/odoo/tools/__init__.py", line 25, in <module>
    from .mail import *
  File "/data/build/odoo/odoo/tools/mail.py", line 32, in <module>
    safe_attrs = clean.defs.safe_attrs | frozenset(
AttributeError: module 'lxml.html.clean' has no attribute 'defs'"""

    def test_make_result_traceback(self):
        self.maxDiff = None
        file_content = f"""
2025-05-04 08:39:00,000 42 INFO other info
2025-05-04 08:40:00,000 42 INFO test_runbot odoo.addons.runbot.tests.test_build_config_step: FAIL: TestMakeResult.test_make_result_traceback
{self.traceback_example}
2024-05-14 09:54:22,692 17 INFO dbname path.to.test: aaa
"""
        with patch('builtins.open', mock_open(read_data=file_content)):
            self.config_step._make_results(self.build)
        self.assertEqual(str(self.build.job_end), '1970-01-01 02:00:00')
        self.assertEqual(self.build.local_result, 'ko')
        expected = f"""Traceback found in logs:
2025-05-04 08:40:00,000 42 INFO test_runbot odoo.addons.runbot.tests.test_build_config_step: FAIL: TestMakeResult.test_make_result_traceback
{self.traceback_example}"""
        self.assertEqual(self.logs, [
            ('INFO', 'Getting results for build %s' % self.build.dest),
            ('ERROR', expected),
        ])

    def test_make_result_traceback_alone(self):
        self.maxDiff = None
        file_content = f"""{self.traceback_example}
2024-05-14 09:54:22,692 17 INFO dbname path.to.test: aaa
"""
        with patch('builtins.open', mock_open(read_data=file_content)):
            self.config_step._make_results(self.build)
        self.assertEqual(str(self.build.job_end), '1970-01-01 02:00:00')
        self.assertEqual(self.build.local_result, 'ko')
        expected = f"""Traceback found in logs:
{self.traceback_example}"""
        self.assertEqual(self.logs, [
            ('INFO', 'Getting results for build %s' % self.build.dest),
            ('ERROR', expected),
        ])

    def test_make_result_traceback_retry(self):
        self.maxDiff = None
        file_content = f"""
2025-05-04 08:39:00,000 42 INFO other info
2025-05-04 08:40:00,000 42 _ERROR test_runbot odoo.addons.runbot.tests.test_build_config_step: FAIL: TestMakeResult.test_make_result_traceback
{self.traceback_example}
2024-05-14 09:54:22,692 17 INFO dbname path.to.test: aaa
2024-05-14 09:54:22,692 17 INFO dbname odoo.modules.loading: Modules loaded.
Some post install stuff
Initiating shutdown
"""
        with patch('builtins.open', mock_open(read_data=file_content)):
            self.config_step._make_results(self.build)
        self.assertEqual(str(self.build.job_end), '1970-01-01 02:00:00')
        self.assertEqual(self.logs, [
            ('INFO', 'Getting results for build %s' % self.build.dest),
        ])
        self.assertEqual(self.build.local_result, 'ok')

    def test_make_result_error(self):
        file_content = """
Loading stuff
odoo.stuff.modules.loading: Modules loaded.
Some post install stuff
2024-05-14 09:54:22,692 17 ERROR dbname path.to.test: FAIL: TestClass.test_
Some log
2024-05-14 09:54:22,692 17 ERROR dbname path.to.test: FAIL: TestClass.test2_
Initiating shutdown
"""
        with patch('builtins.open', mock_open(read_data=file_content)):
            self.config_step._make_results(self.build)
        self.assertEqual(str(self.build.job_end), '1970-01-01 02:00:00')
        self.assertEqual(self.build.local_result, 'ko')
        self.assertEqual(self.logs, [
            ('INFO', 'Getting results for build %s' % self.build.dest),
            ('ERROR', """Error found in logs:
2024-05-14 09:54:22,692 17 ERROR dbname path.to.test: FAIL: TestClass.test_
2024-05-14 09:54:22,692 17 ERROR dbname path.to.test: FAIL: TestClass.test2_"""),
        ])

    def test_make_result_warning(self):
        file_content = """
Loading stuff
odoo.stuff.modules.loading: Modules loaded.
Some post install stuff
2019-12-17 17:34:37,692 17 WARNING dbname path.to.test: timeout exceded
Initiating shutdown
"""
        with patch('builtins.open', mock_open(read_data=file_content)):
            self.config_step._make_results(self.build)
        self.assertEqual(str(self.build.job_end), '1970-01-01 02:00:00')
        self.assertEqual(self.build.local_result, 'warn')
        self.assertEqual(self.logs, [
            ('INFO', 'Getting results for build %s' % self.build.dest),
            ('WARNING', 'Warning found in logs:\n2019-12-17 17:34:37,692 17 WARNING dbname path.to.test: timeout exceded')
        ])

        # no log file
        self.logs = []
        self.patchers['isfile'].return_value = False
        self.config_step._make_results(self.build)

        self.assertEqual(self.build.local_result, 'ko')
        self.assertEqual(self.logs, [
            ('INFO', 'Getting results for build %s' % self.build.dest),
            ('ERROR', 'Log file not found at the end of test job')
        ])

    def test_make_result_already_warn(self):
        file_content = """
Loading stuff
odoo.stuff.modules.loading: Modules loaded.
Some post install stuff
Initiating shutdown
"""
        self.patchers['isfile'].return_value = True
        self.build.local_result = 'warn'
        with patch('builtins.open', mock_open(read_data=file_content)):
            self.config_step._make_results(self.build)
        self.assertEqual(self.logs, [
            ('INFO', 'Getting results for build %s' % self.build.dest)
        ])
        self.assertEqual(str(self.build.job_end), '1970-01-01 02:00:00')
        self.assertEqual(self.build.local_result, 'warn')


    @patch('odoo.addons.runbot.models.build_config.ConfigStep._make_odoo_results')
    def test_make_python_result(self, mock_make_odoo_results):
        config_step = self.ConfigStep.create({
            'name': 'all',
            'job_type': 'python',
            'test_tags': '/module,:class.method',
            'python_result_code': """a = 2*5\nreturn_value = {'local_result': 'ok'}"""
        })
        build = self.Build.create({
            'params_id': self.base_params.id,
        })
        build.local_state = 'testing'
        self.patchers['isfile'].return_value = False
        config_step._make_results(build)
        self.assertEqual(build.local_result, 'ok')

        # invalid result code (no return_value set)
        config_step.python_result_code = """a = 2*5\nr = {'a': 'ok'}\nreturn_value = 'ko'"""
        with self.assertRaises(RunbotException):
            config_step._make_results(build)

        # no result defined
        config_step.python_result_code = ""
        def make_warn(build):
            build.local_result = "warn"

        mock_make_odoo_results.side_effect = make_warn
        config_step._make_results(build)
        self.assertEqual(build.local_result, 'warn')
