from unittest.mock import patch

from odoo import fields
from odoo.tests import HttpCase, tagged


@tagged('-at_install', 'post_install')
class TestRunbotUi(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.project = cls.env['runbot.project'].create({
            'name': 'Runbot UI Tour Project',
        })
        cls.repo = cls.env['runbot.repo'].create({
            'name': 'runbot',
            'project_id': cls.project.id,
            'mode': 'disabled',
        })
        cls.remote = cls.env['runbot.remote'].create({
            'name': 'https://github.com/odoo/runbot.git',
            'repo_id': cls.repo.id,
        })

    def _create_branch(self, **values):
        values['remote_id'] = self.remote.id
        with patch(
            'odoo.addons.runbot.models.branch.Branch._update_branch_infos',
            return_value=None,
        ):
            return self.env['runbot.branch'].create(values)

    def _create_done_build(self, bundle, description, trigger=None):
        config = self.env.ref('runbot.runbot_build_config_default')
        batch = self.env['runbot.batch'].create({
            'bundle_id': bundle.id,
            'state': 'done',
        })
        params_values = {
            'project_id': self.project.id,
            'version_id': bundle.version_id.id,
            'create_batch_id': batch.id,
            'config_id': config.id,
        }
        if trigger:
            params_values['trigger_id'] = trigger.id
        params = self.env['runbot.build.params'].create(params_values)
        build = self.env['runbot.build'].create({
            'params_id': params.id,
            'description': description,
        })
        build.write({
            'local_state': 'done',
            'local_result': 'ok',
        })
        if trigger:
            self.env['runbot.batch.slot'].create({
                'batch_id': batch.id,
                'trigger_id': trigger.id,
                'build_id': build.id,
                'params_id': params.id,
                'link_type': 'created',
            })
        bundle.last_batch = batch
        return build

    def test_frontend_main_flow(self):
        bundle = self.env['runbot.bundle'].create({
            'name': 'ui-tour-active',
            'project_id': self.project.id,
        })
        self._create_branch(
            name='10001',
            pull_head_remote_id=self.remote.id,
            is_pr=True,
            pull_head_name='odoo-dev:ui-tour-active',
            target_branch_name='master',
            alive=True,
        )
        bundle.description = 'Active frontend tour'
        trigger = self.env['runbot.trigger'].create({
            'name': 'Frontend tour trigger',
            'project_id': self.project.id,
            'repo_ids': [(6, 0, self.repo.ids)],
            'config_id': self.env.ref('runbot.runbot_build_config_default').id,
            'batch_dependent': True,
        })
        self._create_done_build(bundle, 'Frontend UI tour build', trigger)

        self.start_tour('/runbot', 'runbot_frontend_main_flow', login='admin')

    def test_error_main_flow(self):
        bundle = self.project.master_bundle_id
        fixing_pr = self._create_branch(
            name='12345',
            is_pr=True,
            pull_head_name='odoo-dev:error-tour',
            target_branch_name='master',
        )
        build = self._create_done_build(bundle, 'UI tour build')
        error = self.env['runbot.build.error'].create({
            'name': 'UI tour error',
            'fixing_pr_id': fixing_pr.id,
            'test_tags': '/runbot:old_failure\n/runbot:shared_context',
        })
        with patch(
            'odoo.addons.runbot.models.build_error.ErrorQualifyRegex._get_cache',
            return_value=self.env['runbot.error.qualify.regex'],
        ):
            error_content = self.env['runbot.build.error.content'].create({
                'error_id': error.id,
                'content': 'UI tour error\nTraceback for the backend tour',
            })
        error_content.qualifiers = {
            'test_class': 'TestRunbotUi',
            'test_method': 'test_error_main_flow',
        }
        self.env['runbot.build.error.link'].create({
            'build_id': build.id,
            'error_content_id': error_content.id,
            'log_date': fields.Datetime.now(),
        })

        self.start_tour('/odoo', 'runbot_error_main_flow', login='admin')

        self.assertEqual(
            error.test_tags,
            '/runbot:new_failure\n/runbot:shared_context',
        )
