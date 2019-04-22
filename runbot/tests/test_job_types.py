
# -*- coding: utf-8 -*-
from time import localtime
from unittest.mock import patch
from odoo.tools.misc import DEFAULT_SERVER_DATETIME_FORMAT
from odoo.tests import common


class Test_Jobs(common.TransactionCase):


    def setUp(self):
        super(Test_Jobs, self).setUp()
        self.Repo = self.env['runbot.repo']
        self.repo = self.Repo.create({'name': 'bla@example.com:foo/bar', 'token': 'xxx'})
        self.Branch = self.env['runbot.branch']
        self.branch_master = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/heads/master',
        })
        self.Build = self.env['runbot.build']
        self.build = self.Build.create({
            'branch_id': self.branch_master.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            'port' : '1234',
        })
