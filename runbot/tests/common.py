# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase
from unittest.mock import patch

class Dummy():
    ...


class RunbotCase(TransactionCase):

    def setUp(self):
        super(RunbotCase, self).setUp()

        self.Build = self.env['runbot.build']
        self.Repo = self.env['runbot.repo']
        self.Branch = self.env['runbot.branch']

        self.patchers = {}

        def git_side_effect(cmd):
            if cmd[:2] == ['show', '-s'] or cmd[:3] == ['show', '--pretty="%H -- %s"', '-s']:
                return 'commit message for %s' % cmd[-1]
            if cmd[:2] == ['cat-file', '-e']:
                return True
            else:
                print('Unsupported mock command %s' % cmd)

        self.start_patcher('git_patcher', 'odoo.addons.runbot.models.repo.runbot_repo._git', side_effect=git_side_effect)
        self.start_patcher('fqdn_patcher', 'odoo.addons.runbot.common.socket.getfqdn', 'host.runbot.com')
        self.start_patcher('github_patcher', 'odoo.addons.runbot.models.repo.runbot_repo._github', {})
        self.start_patcher('is_on_remote_patcher', 'odoo.addons.runbot.models.branch.runbot_branch._is_on_remote', True)
        self.start_patcher('repo_root_patcher', 'odoo.addons.runbot.models.repo.runbot_repo._root', '/tmp/runbot_test/static')
        self.start_patcher('makedirs', 'odoo.addons.runbot.common.os.makedirs', True)
        self.start_patcher('mkdir', 'odoo.addons.runbot.common.os.mkdir', True)
        self.start_patcher('local_pgadmin_cursor', 'odoo.addons.runbot.common.local_pgadmin_cursor', False)  # avoid to create databases
        self.start_patcher('isdir', 'odoo.addons.runbot.common.os.path.isdir', True)
        self.start_patcher('isfile', 'odoo.addons.runbot.common.os.path.isfile', True)
        self.start_patcher('docker_run', 'odoo.addons.runbot.models.build_config.docker_run')
        self.start_patcher('docker_build', 'odoo.addons.runbot.models.build.docker_build')
        self.start_patcher('docker_ps', 'odoo.addons.runbot.models.repo.docker_ps', [])
        self.start_patcher('docker_stop', 'odoo.addons.runbot.models.repo.docker_stop')

    def start_patcher(self, patcher_name, patcher_path, return_value=Dummy, side_effect=Dummy):
        patcher = patch(patcher_path)
        if not hasattr(patcher, 'is_local'):
            res = patcher.start()
            self.addCleanup(patcher.stop)
            self.patchers[patcher_name] = res
            if side_effect != Dummy:
                res.side_effect = side_effect
            elif return_value != Dummy:
                res.return_value = return_value

    def create_build(self, vals):
        return self.Build.create(vals)
