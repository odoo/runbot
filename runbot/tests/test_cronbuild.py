# -*- coding: utf-8 -*-

import coverage
import os
import shutil
import subprocess
import tempfile

from odoo.tests import TransactionCase

COVERAGE_APP = """
def not_used():
    pass

def hello():
    print("Hello world")
    x = False
    if x:
        print("Hello again")

if __name__ == '__main__':
    hello()
"""


class TestRunbotCronBuild(TransactionCase):
    def git(self, *cmd):
        subprocess.call(["git"] + list(cmd), cwd=self.work_tree)

    def setUp(self):
        super().setUp()
        self.tmp_dir = tempfile.mkdtemp()

        @self.addCleanup
        def remove_tmp_dir():
            if os.path.isdir(self.tmp_dir):
                shutil.rmtree(self.tmp_dir)

        self.work_tree = os.path.join(self.tmp_dir, "git_example")
        self.git_dir = os.path.join(self.work_tree, ".git")
        os.mkdir(self.work_tree)
        self.git("init", self.work_tree)
        hooks_dir = os.path.join(self.git_dir, "hooks")
        if os.path.isdir(hooks_dir):
            # Avoid run a hooks for commit commands
            shutil.rmtree(hooks_dir)
        self.repo = self.env["runbot.repo"].create({"name": self.git_dir})
        self.branch = self.env['runbot.branch'].create({
            'name': 'refs/heads/master',
            'branch_name': 'master',
            'repo_id': self.repo.id
        })
        self.build_model = self.env['runbot.build']
        self.cronbuild_model = self.env['runbot.cronbuild']

        @self.addCleanup
        def remove_clone_dir():
            if os.path.isdir(self.repo.path):
                shutil.rmtree(self.repo.path)

    def test_cronbuild_generate_build(self):
        """ Test that a build is generated based on the last build """

        cron_name = 'Nightly build'
        extra_params = '--test-tags nightly'
        cronbuild = self.cronbuild_model.create({
            'name': cron_name,
            'repo_id': self.repo.id,
            'branch_id': self.branch.id,
            'extra_params': extra_params,
        })

        # check that coverage flag is False by default
        self.assertFalse(cronbuild.coverage)

        # check that no new builds are created at bootstrap
        self.cronbuild_model._cron()
        self.assertEqual(self.build_model.search_count([]), 0)

        msg = 'Initial commit'
        self.git("commit", "--allow-empty", "-m", msg)
        self.repo._update_git()
        first_build = self.build_model.search([], order='id')[0]
        self.cronbuild_model._cron()
        last_build = self.build_model.search([], order='id')[-1]

        # check that a new build was generated
        self.assertEqual(self.build_model.search_count([]), 2)
        self.assertNotEqual(first_build, last_build)
        self.assertEqual(last_build.extra_params, extra_params)
        self.assertEqual(last_build.state, 'pending')
        self.assertEqual(last_build.subject, cron_name)
        self.assertFalse(last_build.result)
        self.assertFalse(last_build.coverage)
        self.assertFalse(last_build.coverage_result)

        # change the cronbuild to use coverage
        cronbuild.coverage = True
        # Regenerate a build like if a user click 'run manually' in the cron
        # form
        self.cronbuild_model._cron()
        last_build = self.build_model.search([], order='id')[-1]
        self.assertTrue(last_build.coverage)

    def test_coverage(self):
        """ Test that the coverage result is written on the build """

        hello_path = os.path.join(self.work_tree, 'hello.py')
        with open(hello_path, 'w') as f:
            f.write(COVERAGE_APP)

        subprocess.call(['python3', '-m', 'coverage', 'run', '--branch', 'hello.py'], cwd=self.work_tree)

        cronbuild = self.cronbuild_model.create({
            'name': 'Nightly coverage build',
            'repo_id': self.repo.id,
            'branch_id': self.branch.id,
            'coverage': True,
        })

        self.git("add", "hello.py")
        msg = 'Hello'
        self.git("commit", "-m", msg)

        self.repo._update_git()
        last_build = self.build_model.search([], order='id desc', limit=1)
        branch = self.env['runbot.branch'].search([('id', '=', last_build.branch_id.id)])
        self.assertEqual(branch.coverage_result, 0)

        self.cronbuild_model._cron()

        last_build = self.build_model.search([], order='id desc', limit=1)
        last_build.state = 'done'  # mock a finished build job

        cov = coverage.Coverage(data_file=os.path.join(self.work_tree, '.coverage'))
        cov.load()
        r = cov.report()

        last_build.coverage_result = r
        # check that coverage is stored with 2 digits precision
        self.assertEqual(last_build.coverage_result, round(r, 2))

        # check that the computed field on the branch returns the last build
        # coverage value
        branch.invalidate_cache()
        self.assertEqual(branch.coverage_result, last_build.coverage_result)

        # The last build may be a running build
        last_build.state = 'running'
        branch.invalidate_cache()
        self.assertEqual(branch.coverage_result, last_build.coverage_result)
