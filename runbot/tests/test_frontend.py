# -*- coding: utf-8 -*-
from collections import defaultdict
from itertools import cycle
from unittest.mock import patch
from werkzeug.wrappers import Response
from odoo.tests import common
from odoo.addons.runbot.controllers import frontend
from .common import RunbotCase


class Test_Frontend(RunbotCase):

    def setUp(self):
        super(Test_Frontend, self).setUp()
        Repo = self.env['runbot.repo']
        self.repo = Repo.create({'name': 'bla@example.com:foo/bar', 'token': '123'})
        self.Branch = self.env['runbot.branch']
        self.sticky_branch = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/heads/master',
            'sticky': True,
        })
        self.branch = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/heads/master-test-moc',
            'sticky': False,
        })
        self.Build = self.env['runbot.build']

    @patch('odoo.http.Response.set_default')
    @patch('odoo.addons.runbot.controllers.frontend.request')
    def test_frontend_basic(self, mock_request, mock_set_default):
        mock_request.env = self.env
        mock_request._cr = self.cr
        controller = frontend.Runbot()

        states = ['done', 'pending', 'testing', 'running']
        branches = [self.branch, self.sticky_branch]
        names = ['deadbeef', 'd0d0caca', 'deadface', 'cacafeed']
        # create 5 builds in each branch
        for i, state, branch, name in zip(range(8), cycle(states), cycle(branches), cycle(names)):
            name = '%s%s' % (name, i)
            build = self.Build.create({
                'branch_id': branch.id,
                'name': '%s0000ffffffffffffffffffffffffffff' % name,
                'port': '1234',
                'local_state': state,
                'local_result': 'ok'
                })

        def mocked_simple_repo_render(template, context):
            self.assertEqual(template, 'runbot.repo', 'The frontend controller should use "runbot.repo" template')
            self.assertEqual(self.sticky_branch, context['branches'][0]['branch'], "The sticky branch should be in first place")
            self.assertEqual(self.branch, context['branches'][1]['branch'], "The non sticky branch should be in second place")
            self.assertEqual(len(context['branches'][0]['builds']), 4, "Only the 4 last builds should appear in the context")
            self.assertEqual(context['pending'], 2, "There should be 2 pending builds")
            self.assertEqual(context['running'], 2, "There should be 2 running builds")
            self.assertEqual(context['testing'], 2, "There should be 2 testing builds")
            self.assertEqual(context['pending_total'], 2, "There should be 2 pending builds")
            self.assertEqual(context['pending_level'], 'info', "The pending level should be info")
            return Response()

        mock_request.render = mocked_simple_repo_render
        controller.repo(repo=self.repo)

        def mocked_repo_search_render(template, context):
            dead_count = len([bu['name'] for b in context['branches'] for bu in b['builds'] if bu['name'].startswith('dead')])
            undead_count = len([bu['name'] for b in context['branches'] for bu in b['builds'] if not bu['name'].startswith('dead')])
            self.assertEqual(dead_count, 4, 'The search for "dead" should return 4 builds')
            self.assertEqual(undead_count, 0, 'The search for "dead" should not return any build without "dead" in its name')
            return Response()

        mock_request.render = mocked_repo_search_render
        controller.repo(repo=self.repo, search='dead')
