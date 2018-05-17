
import os
import shutil
import subprocess
import tempfile

from odoo.tests import TransactionCase


class TestRunbotSkipBuild(TransactionCase):
    def setUp(self):
        super().setUp()
        self.tmp_dir = tempfile.mkdtemp()

        @self.addCleanup
        def remove_tmp_dir():
            if os.path.isdir(self.tmp_dir):
                shutil.rmtree(self.tmp_dir)

        self.work_tree = os.path.join(self.tmp_dir, "git_example")
        self.git_dir = os.path.join(self.work_tree, ".git")
        subprocess.call(["git", "init", self.work_tree])
        hooks_dir = os.path.join(self.git_dir, "hooks")
        if os.path.isdir(hooks_dir):
            # Avoid run a hooks for commit commands
            shutil.rmtree(hooks_dir)
        self.repo = self.env["runbot.repo"].create({"name": self.git_dir})
        self.build = self.env["runbot.build"]

        @self.addCleanup
        def remove_clone_dir():
            if os.path.isdir(self.repo.path):
                shutil.rmtree(self.repo.path)

    def git(self, *cmd):
        subprocess.call(["git"] + list(cmd), cwd=self.work_tree)

    def test_subject_skip_build(self):
        """Test [ci skip] feature"""

        cimsg = "Testing subject [ci skip]"
        self.git("commit", "--allow-empty", "-m", cimsg)
        self.repo._update_git()
        build = self.build.search([("subject", "=", cimsg)])
        self.assertFalse(build)

        cimsg = "Testing subject without ci skip"
        self.git("commit", "--allow-empty", "-m", cimsg)
        self.repo._update_git()
        build = self.build.search([("subject", "=", cimsg)])
        self.assertTrue(build)

        cimsg = "Testing body\n\n[ci skip]\nother line"
        self.git("commit", "--allow-empty", "-m", cimsg)
        self.repo._update_git()
        build = self.build.search([("subject", "=", cimsg.split("\n")[0])])
        self.assertFalse(build)

        cimsg = "Testing body without\n\nci skip\nother line"
        self.git("commit", "--allow-empty", "-m", cimsg)
        self.repo._update_git()
        build = self.build.search([("subject", "=", cimsg.split("\n")[0])])
        self.assertTrue(build)
