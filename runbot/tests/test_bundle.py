# -*- coding: utf-8 -*-
from .common import RunbotCase

class TestBundle(RunbotCase):

    def test_pull_request_labels(self):
        mock_github = self.patchers['github_patcher']
        mock_github.return_value = {
            'base': {'ref': 'master'},
            'head': {'label': 'foo-dev:bar_branch', 'repo': {'full_name': 'dev/server'}},
            'labels': [{
                'name': 'test 14.4',
                'color': 'ededed',
            }, {
                'name': 'test forwardport',
                'color': 'a4fcde',
            }]
        }

        # create a pre-existing label to test unique constraint
        self.env['runbot.branch.label'].create([{'name': 'test forwardport'}])

        # create a dev branch and a PR
        dev_branch = self.Branch.create({
            'remote_id': self.remote_server_dev.id,
            'name': 'bar_branch',
            'is_pr': False
        })

        pr = self.Branch.create({
            'remote_id': self.remote_server.id,
            'name': '12345',
            'is_pr': True,
        })
        self.assertEqual(pr.name, '12345')
        self.assertEqual(pr.branch_url, 'https://example.com/base/server/pull/12345')
        self.assertEqual(pr.target_branch_name, 'master')
        self.assertEqual(pr.pull_head_name, 'foo-dev:bar_branch')

        # test that the bundle was created with branch and PR
        bundle = dev_branch.bundle_id
        self.assertIn(pr, bundle.branch_ids)

        # test the labels
        labels = self.env['runbot.branch.label'].search([('name', 'like', 'test %')])
        self.assertEqual(len(labels), 2)
        self.assertEqual(labels, pr.label_ids)
        self.assertEqual(bundle.labels, pr.label_ids)

        # create an addon branch and Pr
        mock_github.return_value = {
            'base': {'ref': 'master'},
            'head': {'label': 'foo-dev:bar_branch', 'repo': {'full_name': 'dev/addons'}},
            'labels': [{
                'name': 'foo label',
                'color': 'ededed',
            }, {
                'name': 'test forwardport',
                'color': 'a4fcde',
            }]
        }

        addon_dev_branch = self.Branch.create({
            'remote_id': self.remote_addons_dev.id,
            'name': 'bar_branch',
            'is_pr': False
        })
        self.assertEqual(addon_dev_branch.bundle_id, bundle)

        # create 2 PR to test that 2 NEW `foo label` same labels appearing at the same time
        addon_pr, _ = self.Branch.create([{
            'remote_id': self.remote_addons.id,
            'name': '6789',
            'is_pr': True}, {
            'remote_id': self.remote_addons.id,
            'name': '6790',
            'is_pr': True
        }])
        self.assertEqual(addon_pr.bundle_id, bundle)
        self.assertEqual(len(bundle.branch_ids), 5)

        # now test that labels are correctly set on the bundle
        self.assertEqual(3, len(bundle.labels))
        self.assertIn('foo label', bundle.labels.mapped('name'))
        self.assertIn('test forwardport', bundle.labels.mapped('name'))
        self.assertIn('test 14.4', bundle.labels.mapped('name'))

        # check that bundle labels can be searched
        fw_port_bundle_ids = self.env['runbot.bundle'].search([('labels', '=', 'test forwardport')])
        self.assertEqual(fw_port_bundle_ids, bundle)
        test_bundle_ids = self.env['runbot.bundle'].search([('labels', 'ilike', 'test%')])
        self.assertEqual(test_bundle_ids, bundle)
        foo_bundle_ids = self.env['runbot.bundle'].search([('labels', 'in', ['test forwardport', 'foo label'])])
        self.assertEqual(foo_bundle_ids, bundle)

        # check labels bundle
        fw_label = self.env['runbot.branch.label'].search([('name', '=', 'test forwardport')])
        self.assertIn(bundle, fw_label.bundle_ids)
