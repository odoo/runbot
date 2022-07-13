import json

from odoo import fields
from odoo.tests.common import HttpCase

from .common import RunbotCase


class TestJsonRoutes(RunbotCase, HttpCase):

    def setUp(self):
        super().setUp()
        self.additionnal_setup()
        self.private_group = self.env['res.groups'].create({
            'name': 'Test Private',
        })
        self.private_project = self.Project.create({
            'name': 'Private Test Project',
            'group_ids': [fields.Command.link(self.private_group.id)],
        })

        self.private_repo_server = self.Repo.create({
            'name': 'private_server',
            'project_id': self.private_project.id,
            'server_files': 'server.py',
            'addons_paths': 'addons,core/addons'
        })

        self.private_remote_server = self.Remote.create({
            'name': 'foo@example.com:base/server',
            'repo_id': self.private_repo_server.id,
            'token': '456',
        })

        self.initial_private_server_commit = self.Commit.create({
            'name': 'afafafaf',
            'repo_id': self.private_repo_server.id,
            'date': '2020-01-01',
            'subject': 'Initial Private Commit',
            'author': 'foofoo',
            'author_email': 'foofoo@somewhere.com'
        })

        self.private_branch_server = self.Branch.create({
            'name': '13.0',
            'remote_id': self.private_remote_server.id,
            'is_pr': False,
            'head': self.initial_private_server_commit.id,
        })
        self.branch_server.bundle_id.is_base = True

        self.Trigger.create({
            'name': 'Private Repo Server trigger',
            'repo_ids': [(4, self.private_repo_server.id)],
            'config_id': self.default_config.id,
            'project_id': self.private_project.id,
        })

    def check_json_route(self, route, expected_status):
        response = self.url_open(route)
        self.assertEqual(response.status_code, expected_status)
        res = json.loads(response.content)
        if expected_status == 403:
            self.assertEqual(res, 'unauthorized')
        return res

    def test_json_flow_public_user(self):
        # test that a public user can get public project informations
        projects_infos = self.check_json_route('/runbot/json/projects', 200)
        project_names = [p['name'] for p in projects_infos]
        self.assertIn(self.project.name, project_names)

        # test that a public user cannot get a private project informations
        self.assertNotIn('Private Project', project_names)
        self.check_json_route(f'/runbot/json/projects/{self.private_project.id}', 403)

        bundles_infos = self.check_json_route(projects_infos[0]['bundles_url'], 200)
        bundles_names = [b['name'] for b in bundles_infos]
        self.assertIn(self.branch_server.bundle_id.name, bundles_names)

        # check that public user cannot access private project bundles
        private_bundle = self.Bundle.search([('name', '=', '13.0'), ('project_id', '=', self.private_project.id)])
        self.check_json_route(f'/runbot/json/bundles/{private_bundle.id}', 403)

        private_batch = self.private_branch_server.bundle_id._force()
        private_batch._prepare()
        batches_infos = self.check_json_route(f'/runbot/json/bundles/{self.branch_server.bundle_id.id}/batches', 200)
        batch_ids = [ b['id'] for b in batches_infos]
        self.assertEqual(len(batch_ids), 1)
        self.assertNotIn(private_batch.id, batch_ids)

        # Let's verify that the batches infos contains the commits informations too
        for commit in batches_infos[0]['commits']:
            self.assertIn(commit['hash'], ['aaaaaaa', 'cccccc'])

        # check that a public user cannot access private project batches
        private_batches_infos = self.check_json_route(f'/runbot/json/bundles/{self.private_branch_server.bundle_id.id}/batches', 200)
        self.assertEqual(private_batches_infos, [])

        commits_infos = self.check_json_route(batches_infos[0]['commits_url'], 200)
        self.check_json_route(commits_infos[0]['url'], 200)

        # check that a public user cannot access private project commits
        self.check_json_route(f'/runbot/json/batches/{private_batch.id}/commits', 403)
        self.check_json_route(f'/runbot/json/commits/{self.initial_private_server_commit.id}', 403)

        commit_links_infos = self.check_json_route(commits_infos[0]['commit_links_url'], 200)
        self.check_json_route(commit_links_infos[0]['url'], 200)

        # check that a public user cannot access private project commit links
        private_commit_link = self.env['runbot.commit.link'].search([('commit_id', '=', self.initial_private_server_commit.id)], limit=1)
        self.check_json_route(f'/runbot/json/commit_links/{private_commit_link.id}', 403)

        server_slot_infos = list(filter(lambda slot: slot['trigger_name'] == 'Server trigger', self.check_json_route(batches_infos[0]['slots_url'], 200)))

        # check that a public user cannot access private project batch slots
        self.check_json_route(f'/runbot/json/batches/{private_batch.id}/slots', 403)
        private_slot = self.env['runbot.batch.slot'].search([('batch_id', '=', private_batch.id)], limit=1)
        self.check_json_route(f'/runbot/json/batch_slots/{private_slot.id}', 403)

        server_build_infos = self.check_json_route(server_slot_infos[0]['build_url'], 200)
        self.assertEqual(server_build_infos[0]['trigger'], 'Server trigger')

        # check that a public user cannot access private project build
        self.check_json_route(f'/runbot/json/batches/{private_batch.id}/builds', 403)
        private_build = self.env['runbot.build'].search([('params_id.project_id','=', self.private_project.id)])
        self.check_json_route(f'/runbot/json/builds/{private_build.id}', 403)
