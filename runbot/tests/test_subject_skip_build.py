
import os
import shutil
import subprocess
import tempfile

from odoo.tests import TransactionCase


class TestRunbotSkipBuild(TransactionCase):
    def setUp(self):
        super().setUp()
        self.tmp_dir = tempfile.mkdtemp()
        self.work_tree = os.path.join(self.tmp_dir, "git_example")
        self.git_dir = os.path.join(self.work_tree, ".git")
        subprocess.call(["git", "init", self.work_tree])
        hooks_dir = os.path.join(self.git_dir, "hooks")
        if os.path.isdir(hooks_dir):
            # Avoid run a hooks for commit commands
            shutil.rmtree(hooks_dir)

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.tmp_dir)

    def git(self, *cmd):
        subprocess.call(["git"] + list(cmd), cwd=self.work_tree)

    def test_subject_skip_build(self):
        """Test [ci skip] feature"""
        repo = self.env["runbot.repo"].create({"name": self.git_dir})

        cimsg = "Testing subject [ci skip]"
        self.git("commit", "--allow-empty", "-m", cimsg)
        repo._update_git()
        build = self.env["runbot.build"].search([("subject", "=", cimsg)])
        self.assertFalse(build)

        cimsg = "Testing subject without ci skip"
        self.git("commit", "--allow-empty", "-m", cimsg)
        repo._update_git()
        build = self.env["runbot.build"].search([("subject", "=", cimsg)])
        self.assertTrue(build)

        cimsg = "Testing body\n\n[ci skip]"
        self.git("commit", "--allow-empty", "-m", cimsg)
        repo._update_git()
        build = self.env["runbot.build"].search([
            ("subject", "=", cimsg.split("\n")[0])])
        self.assertFalse(build)

        cimsg = "Testing body without\n\nci skip"
        self.git("commit", "--allow-empty", "-m", cimsg)
        repo._update_git()
        build = self.env["runbot.build"].search([
            ("subject", "=", cimsg.split("\n")[0])])
        self.assertTrue(build)
