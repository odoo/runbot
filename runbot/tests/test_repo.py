# -*- coding: utf-8 -*-
from unittest.mock import patch
from odoo.exceptions import ValidationError
from odoo.tests import common

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

    def test_duplicate_repo_cross_reference(self):
        """ Test that a repo is not cross referenced in its duplicate repo """
        repo = self.Repo.create({'name': 'bla@example.com:foo/bar'})
        repo_dev = self.Repo.create({'name': 'bla@example.com:foo-dev/bar'})

        repo.duplicate_id = repo_dev.id
        with self.assertRaises(ValidationError):
            repo_dev.duplicate_id = repo.id
