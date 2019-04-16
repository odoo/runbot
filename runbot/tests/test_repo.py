# -*- coding: utf-8 -*-
from unittest.mock import patch
from odoo.tests import common
import odoo

class Test_Repo(common.TransactionCase):

    def setUp(self):
        super(Test_Repo, self).setUp()
        self.Repo = self.env['runbot.repo']

    @patch('odoo.addons.runbot.models.repo.runbot_repo._root')
    def test_base_fields(self, mock_root):
        mock_root.return_value = '/tmp/static'
        repo = self.Repo.create({'name': 'bla@example.com:foo/bar'})
        self.assertEqual(repo.path, '/tmp/static/repo/bla_example.com_foo_bar')

        self.assertEqual(repo.base, 'example.com/foo/bar')
        self.assertEqual(repo.short_name, 'foo/bar')

        https_repo = self.Repo.create({'name': 'https://bla@example.com/user/rep.git'})
        self.assertEqual(https_repo.short_name, 'user/rep')

        local_repo = self.Repo.create({'name': '/path/somewhere/rep.git'})
        self.assertEqual(local_repo.short_name, 'somewhere/rep')


class Test_Repo_Scheduler(common.TransactionCase):

    @patch('odoo.addons.runbot.models.repo.runbot_repo._root')
    def setUp(self, mock_root):
        # as the _scheduler method commits, we need to protect the database
        registry = odoo.registry()
        registry.enter_test_mode()
        self.addCleanup(registry.leave_test_mode)
        super(Test_Repo_Scheduler, self).setUp()

        mock_root.return_value = '/tmp/static'
        self.Repo_model = self.env['runbot.repo']
        self.Branch_model = self.env['runbot.branch']
        self.foo_repo = self.Repo_model.create({'name': 'bla@example.com:foo/bar'})

        self.foo_branch = self.Branch_model.create({
            'repo_id': self.foo_repo.id,
            'name': 'refs/head/foo'
        })

    @patch('odoo.addons.runbot.models.build.runbot_build._reap')
    @patch('odoo.addons.runbot.models.build.runbot_build._kill')
    @patch('odoo.addons.runbot.models.build.runbot_build._schedule')
    @patch('odoo.addons.runbot.models.repo.fqdn')
    def test_repo_scheduler(self, mock_repo_fqdn, mock_schedule, mock_kill, mock_reap):
        mock_repo_fqdn.return_value = 'test_host'
        Build_model = self.env['runbot.build']
        builds = []
        # create 6 builds that are testing on the host to verify that
        # workers are not overfilled
        for build_name in ['a', 'b', 'c', 'd', 'e', 'f']:
            build = Build_model.create({
                'branch_id': self.foo_branch.id,
                'name': build_name,
                'port': '1234',
                'build_type': 'normal',
                'state': 'testing',
                'host': 'test_host'
            })
            builds.append(build)
        # now the pending build that should stay unasigned
        scheduled_build = Build_model.create({
            'branch_id': self.foo_branch.id,
            'name': 'sched_build',
            'port': '1234',
            'build_type': 'scheduled',
            'state': 'pending',
        })
        builds.append(scheduled_build)
        # create the build that should be assigned once a slot is available
        build = Build_model.create({
            'branch_id': self.foo_branch.id,
            'name': 'foobuild',
            'port': '1234',
            'build_type': 'normal',
            'state': 'pending',
        })
        builds.append(build)
        self.env['runbot.repo']._scheduler(ids=[self.foo_repo.id, ])

        build.invalidate_cache()
        scheduled_build.invalidate_cache()
        self.assertFalse(build.host)
        self.assertFalse(scheduled_build.host)

        # give some room for the pending build
        Build_model.search([('name', '=', 'a')]).write({'state': 'done'})

        self.env['runbot.repo']._scheduler(ids=[self.foo_repo.id, ])
        build.invalidate_cache()
        scheduled_build.invalidate_cache()
        self.assertEqual(build.host, 'test_host')
        self.assertFalse(scheduled_build.host)
