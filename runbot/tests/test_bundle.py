# -*- coding: utf-8 -*-
from .common import RunbotCase

class TestBundle(RunbotCase):

    def test_pull_request_pr_state(self):
        # create a dev branch
        dev_branch = self.Branch.create({
            'remote_id': self.remote_server_dev.id,
            'name': 'bar_branch',
            'is_pr': False
        })

        bundle = dev_branch.bundle_id
        self.assertEqual(bundle.name, 'bar_branch')
        self.assertEqual(bundle.pr_state, 'nopr', 'The bundle should be in `nopr` state')
        self.assertIn(bundle, self.Bundle.search([('pr_state', '=', 'nopr')]))

        # now create a PR and check that the bundle pr_state is `open`
        mock_github = self.patchers['github_patcher']
        mock_github.return_value = {
            'base': {'ref': 'master'},
            'head': {'label': 'foo-dev:bar_branch', 'repo': {'full_name': 'dev/server'}},
        }

        pr = self.Branch.create({
            'remote_id': self.remote_server.id,
            'name': '12345',
            'is_pr': True,
            'alive': True
        })

        self.env['runbot.branch'].flush()  # Needed to test sql query in _search_pr_state

        self.assertIn(pr, bundle.branch_ids)
        self.assertEqual(bundle.pr_state, 'open')

        # test the different searches operators
        self.assertIn(bundle, self.Bundle.search([('pr_state', '=', 'open')]))
        self.assertNotIn(bundle, self.Bundle.search([('pr_state', '=', 'done')]))
        self.assertNotIn(bundle, self.Bundle.search([('pr_state', '!=', 'open')]))
        self.assertIn(bundle, self.Bundle.search([('pr_state', '!=', 'done')]))

        # add a new PR from another repo in the same bundle (mimic enterprise PR)
        mock_github.return_value = {
            'base': {'ref': 'master'},
            'head': {'label': 'bar-dev:bar_branch', 'repo': {'full_name': 'dev/addons'}},
        }

        addons_pr = self.Branch.create({
            'remote_id': self.remote_addons.id,
            'name': '6789',
            'is_pr': True,
            'alive': True
        })

        self.assertIn(addons_pr, bundle.branch_ids)
        self.assertEqual(bundle.pr_state, 'open')
        self.assertIn(bundle, self.Bundle.search([('pr_state', '=', 'open')]))
        self.assertNotIn(bundle, self.Bundle.search([('pr_state', '=', 'done')]))

        # one PR is closed, the bundle pr_state should stay open
        addons_pr.alive = False
        self.env['runbot.branch'].flush()  # Needed to test sql query in _search_pr_state
        self.assertEqual(bundle.pr_state, 'open')
        self.assertIn(bundle, self.Bundle.search([('pr_state', '=', 'open')]))
        self.assertNotIn(bundle, self.Bundle.search([('pr_state', '=', 'done')]))

        # The last PR is closed so the bundle pr state should be done
        pr.alive = False
        self.env['runbot.branch'].flush()  # Needed to test sql query in _search_pr_state
        self.assertEqual(bundle.pr_state, 'done')
        self.assertNotIn(bundle, self.Bundle.search([('pr_state', '=', 'open')]))
        self.assertIn(bundle, self.Bundle.search([('pr_state', '=', 'done')]))
