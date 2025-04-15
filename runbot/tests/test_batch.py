from datetime import timedelta

from odoo import fields

from .common import RunbotCase


class TestBatch(RunbotCase):

    def test_process_delay(self):
        self.project.process_delay = 120
        self.additionnal_setup()

        batch = self.branch_addons.bundle_id.last_batch
        batch._process()
        self.assertEqual(batch.state, 'preparing')

        batch.last_update = fields.Datetime.now() - timedelta(seconds=120)
        batch._process()
        self.assertEqual(batch.state, 'ready')

    def test_build_link(self):
        self.trigger_addons.unlink()
        self.trigger_server.ci_context = "test"

        def get_build_commit(sha, tree_hash, branch):
            commit = self.Commit._get(sha, self.repo_server.id, {
                'tree_hash': tree_hash,
            })
            branch.head = commit
            batch = self.env['runbot.batch'].create({
                'last_update': fields.Datetime.now(),
                'bundle_id': branch.bundle_id.id,
                'state': 'preparing',
            })
            branch.bundle_id.last_batch = batch
            batch._process()
            self.assertEqual(batch.commit_link_ids.commit_id, commit)
            return batch, batch.slot_ids.build_id, commit

        batch_1, build_1, commit_1 = get_build_commit('aaaaaaa', '0aaaaaa', self.branch_server)
        self.assertEqual(build_1.slot_ids.mapped('batch_id'), batch_1)

        batch_2, build_2, commit_2 = get_build_commit('bbbbbbb', '0bbbbbb', self.branch_server)
        self.assertNotEqual(build_1, build_2)
        self.assertNotEqual(commit_1, commit_2)
        self.assertNotEqual(batch_1, batch_2)
        self.assertEqual(build_2.slot_ids.mapped('batch_id'), batch_2)

        batch_3, build_2b, commit_2b = get_build_commit('bbbbbbb', '0bbbbbb', self.dev_branch)
        self.assertEqual(build_2, build_2b)
        self.assertEqual(commit_2, commit_2b)
        self.assertNotEqual(batch_2, batch_3)
        self.assertEqual(build_2.slot_ids.mapped('batch_id'), batch_2 | batch_3)

        batch_4, build_2c, commit_4 = get_build_commit('bbbbbb2', '0bbbbbb', self.dev_branch)
        self.assertEqual(build_2, build_2c)
        self.assertNotEqual(commit_2, commit_4)
        self.assertEqual(commit_2.tree_hash, commit_4.tree_hash)
        self.assertEqual(build_2.slot_ids.mapped('batch_id'), batch_2 | batch_3 | batch_4)

        # build seen from batch 2 and 3
        self.assertEqual(build_2.params_id._get_batch_commit_link_ids(batch_2).commit_id, commit_2)
        self.assertEqual(build_2.params_id._get_batch_commit_link_ids(batch_3).commit_id, commit_2)
        self.assertEqual(build_2.params_id._get_batch_commit_link_ids(batch_2).commit_id.name, 'bbbbbbb')
        # build seen from batch 4
        self.assertEqual(build_2.params_id._get_batch_commit_link_ids(batch_4).commit_id, commit_4)
        self.assertEqual(build_2.params_id._get_batch_commit_link_ids(batch_4).commit_id.name, 'bbbbbb2')

        def assert_status_info(commit):
            infos = commit._get_last_statuses()[1]['test']
            parts = infos.target_url.split('/')
            return {
                'batch_id': int(parts[-3]),
                'build_id': int(parts[-1]),
                'state': infos.state,
            }

        self.assertEqual(list(assert_status_info(commit_1).values()), [batch_1.id, build_1.id, 'pending'])
        self.assertEqual(list(assert_status_info(commit_2).values()), [batch_2.id, build_2.id, 'pending'])
        self.assertEqual(list(assert_status_info(commit_2b).values()), [batch_2.id, build_2.id, 'pending'])
        self.assertEqual(list(assert_status_info(commit_4).values()), [batch_4.id, build_2.id, 'pending'])

        # check that status is updated
        build_1.local_result = 'ok'
        build_1.local_state = 'done'
        self.assertEqual(list(assert_status_info(commit_1).values()), [batch_1.id, build_1.id, 'success'])
        build_2.local_result = 'ko'
        build_2.local_state = 'done'
        self.assertEqual(list(assert_status_info(commit_2).values()), [batch_2.id, build_2.id, 'failure'])  # batch_2 or batch_3 could make sense
        self.assertEqual(list(assert_status_info(commit_4).values()), [batch_4.id, build_2.id, 'failure'])
