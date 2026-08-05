from datetime import datetime, timedelta

from .common import RunbotCase


class TestBatch(RunbotCase):

    def test_process_delay(self):
        self.project.process_delay = 120
        self.additionnal_setup()

        batch = self.branch_addons.bundle_id.last_batch
        batch._process()
        self.assertEqual(batch.state, 'preparing')

        batch.last_update = datetime.now() - timedelta(seconds=120)
        batch._process()
        self.assertEqual(batch.state, 'ready')

    def test_build_link(self):
        self.trigger_addons.unlink()
        self.trigger_server.ci_context = "test"

        def get_build_commit(sha, tree_hash, branch):
            commit = self.Commit._get(sha, self.repo_odoo.id, {
                'tree_hash': tree_hash,
            })
            branch.head = commit
            batch = self.env['runbot.batch'].create({
                'last_update': datetime.now(),
                'bundle_id': branch.bundle_id.id,
                'state': 'preparing',
            })
            branch.bundle_id.last_batch = batch
            batch._process()
            self.assertEqual(batch.commit_link_ids.commit_id, commit)
            return batch, batch.slot_ids.build_id, commit

        batch_1, build_1, commit_1 = get_build_commit('aaaaaaa', '0aaaaaa', self.branch_odoo)
        self.assertEqual(build_1.slot_ids.mapped('batch_id'), batch_1)

        batch_2, build_2, commit_2 = get_build_commit('bbbbbbb', '0bbbbbb', self.branch_odoo)
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
            self.env.cr.precommit.run()
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
        self.assertEqual(list(assert_status_info(commit_2).values()), [batch_2.id, build_2.id, 'error'])  # batch_2 or batch_3 could make sense
        self.assertEqual(list(assert_status_info(commit_4).values()), [batch_4.id, build_2.id, 'error'])


class TestBatchSkip(RunbotCase):
    """
    !! This test class was mostly AI written

    When a new batch replaces an older one, builds of the older batch must be
    skipped (pending) or marked killable (waiting/testing) *unless* they are
    still genuinely needed by another batch.

    A build is still needed when it is attached to an unfinished batch
    (`preparing` or `ready`) and either:
      - that batch is the last batch of its bundle, or
      - that batch belongs to a base bundle. A build attached to a base bundle
        is always needed, whether or not the batch is the last one.
    """

    def setUp(self):
        super().setUp()
        self.master_bundle.is_base = True
        self.other_bundle = self.Bundle.create({
            'name': 'master-dev-other',
            'project_id': self.project.id,
        })
        # Params shared by several batches. This happens for real when a batch is
        # created because some repo moved, while the commits relevant to this
        # trigger did not change (same tree hashes -> same params fingerprint).
        shared_commit = self.Commit.create({
            'name': 'shared_sha',
            'tree_hash': 'shared_sha',
            'repo_id': self.repo_odoo.id,
        })
        self.shared_params = self.BuildParameters.create({
            'project_id': self.project.id,
            'commit_link_ids': [(0, 0, {'commit_id': shared_commit.id})],
        })
        other_commit = self.Commit.create({
            'name': 'other_sha',
            'tree_hash': 'other_sha',
            'repo_id': self.repo_odoo.id,
        })
        self.other_params = self.BuildParameters.create({
            'project_id': self.project.id,
            'commit_link_ids': [(0, 0, {'commit_id': other_commit.id})],
        })

    def _create_batch(self, bundle, state, params=None, build=None,
                      link_type='created', is_last=True):
        batch = self.Batch.create({
            'bundle_id': bundle.id,
            'state': state,
            'last_update': datetime.now(),
        })
        if params:
            self.env['runbot.batch.slot'].create({
                'batch_id': batch.id,
                'trigger_id': self.trigger_server.id,
                'params_id': params.id,
                'build_id': build.id if build else False,
                'link_type': link_type,
            })
        if is_last:
            bundle.last_batch = batch
        return batch

    def assertNotKilled(self, build, msg=None):
        self.assertFalse(build.killable, msg or 'build should not have been marked killable')
        self.assertNotEqual(build.local_result, 'skipped')

    def test_skip_killable_when_other_slot_is_on_a_done_batch(self):
        """A slot on an already-done batch must not protect a build.

        batch_1 had a red minimal check, so the slot for the heavy trigger got a
        params but never a build; batch_1 was then marked `done` and `_skip` was
        never given a chance to flag its slots as skipped. The build created in
        batch_2 must still become killable when batch_3 replaces batch_2.
        """
        batch_1 = self._create_batch(self.dev_bundle, 'done', params=self.shared_params, build=None, is_last=False)

        build = self.Build.create({'params_id': self.shared_params.id, 'local_state': "testing", 'global_state': "testing"})
        batch_2 = self._create_batch(self.dev_bundle, 'ready', params=self.shared_params, build=build)

        # batch_3 arrives with its own params and skips batch_2
        self._create_batch(self.dev_bundle, 'preparing', params=self.other_params)
        batch_2._skip()

        self.assertEqual(batch_2.state, 'skipped')
        self.assertFalse(batch_1.slot_ids.skipped, 'a done batch keeps its slots untouched')
        self.assertTrue(batch_2.slot_ids.skipped)
        self.assertTrue(build.killable, 'build should have been marked killable')

    def test_skip_pending_build_when_other_slot_is_on_a_done_batch(self):
        """Same scenario, but the build never started: it must be skipped."""
        self._create_batch(self.dev_bundle, 'done', params=self.shared_params, build=None, is_last=False)

        pending_build = self.Build.create({'params_id': self.shared_params.id})
        batch_2 = self._create_batch(self.dev_bundle, 'ready', params=self.shared_params, build=pending_build)

        batch_2._skip()

        self.assertEqual(pending_build.local_state, 'done')
        self.assertEqual(pending_build.local_result, 'skipped')

    def test_skip_killable_when_other_slot_is_on_a_skipped_batch(self):
        """Variant: the sibling batch was skipped rather than done."""
        self._create_batch(self.dev_bundle, 'skipped', params=self.shared_params, build=None, is_last=False)

        build = self.Build.create({'params_id': self.shared_params.id, 'local_state': "testing", 'global_state': "testing"})
        batch_2 = self._create_batch(self.dev_bundle, 'ready', params=self.shared_params, build=build)

        batch_2._skip()
        self.assertTrue(build.killable, 'build should have been marked killable')

    def test_skip_killable_when_other_slot_batch_is_not_the_last_one(self):
        """A running batch that is no longer the last of its bundle is stale.

        Only the `last_batch` intersection catches this one: the batch holding
        the build is still `ready`, so filtering on the state alone is not
        enough.
        """
        build = self.Build.create({'params_id': self.shared_params.id, 'local_state': "testing", 'global_state': "testing"})
        other_batch = self._create_batch(self.other_bundle, 'ready', params=self.shared_params, build=build, link_type='matched')
        # a newer batch became the last one of the other bundle
        newer = self._create_batch(self.other_bundle, 'preparing')
        self.assertEqual(self.other_bundle.last_batch, newer)
        self.assertEqual(other_batch.state, 'ready')

        batch = self._create_batch(self.dev_bundle, 'ready', params=self.shared_params, build=build)
        batch._skip()

        self.assertTrue(build.killable, 'build should have been marked killable')

    # ------------------------------------------------------------------
    # non regression: builds that must survive
    # ------------------------------------------------------------------
    def test_skip_keeps_build_used_by_newer_batch_of_same_bundle(self):
        """The build was relinked by the batch that replaces this one."""
        build = self.Build.create({'params_id': self.shared_params.id, 'local_state': "testing", 'global_state': "testing"})
        batch_2 = self._create_batch(self.dev_bundle, 'ready', params=self.shared_params, build=build, is_last=False)
        batch_3 = self._create_batch(self.dev_bundle, 'ready', params=self.shared_params, build=build, link_type='matched')
        self.assertEqual(self.dev_bundle.last_batch, batch_3)

        batch_2._skip()

        self.assertNotKilled(build)
        self.assertFalse(batch_3.slot_ids.skipped)

    def test_skip_keeps_build_used_by_last_batch_of_another_bundle(self):
        """The build is shared with another bundle that still needs it."""
        build = self.Build.create({'params_id': self.shared_params.id, 'local_state': "testing", 'global_state': "testing"})
        other_batch = self._create_batch(self.other_bundle, 'ready', params=self.shared_params, build=build, link_type='matched')
        self.assertEqual(self.other_bundle.last_batch, other_batch)

        batch = self._create_batch(self.dev_bundle, 'ready', params=self.shared_params, build=build)
        batch._skip()

        self.assertNotKilled(build)

    def test_skip_keeps_build_used_by_a_preparing_batch(self):
        """`preparing` is as valid as `ready` for keeping a build alive."""
        build = self.Build.create({'params_id': self.shared_params.id, 'local_state': "testing", 'global_state': "testing"})
        self._create_batch(self.other_bundle, 'preparing', params=self.shared_params, build=build, link_type='matched')

        batch = self._create_batch(self.dev_bundle, 'ready', params=self.shared_params, build=build)
        batch._skip()

        self.assertNotKilled(build)

    def test_skip_keeps_build_used_by_a_base_bundle(self):
        """Builds linked to a base bundle are never killed.

        The base batch is deliberately *not* the last batch of the base bundle,
        so the `last_batch` intersection would kill the build without the
        explicit `is_base` guard.
        """
        build = self.Build.create({'params_id': self.shared_params.id, 'local_state': "testing", 'global_state': "testing"})
        base_batch = self._create_batch(self.master_bundle, 'ready', params=self.shared_params, build=build, link_type='matched')
        newer_base_batch = self._create_batch(self.master_bundle, 'preparing')
        self.assertEqual(self.master_bundle.last_batch, newer_base_batch)
        self.assertNotEqual(base_batch, newer_base_batch)

        batch = self._create_batch(self.dev_bundle, 'ready', params=self.shared_params, build=build)
        batch._skip()

        self.assertNotKilled(build)

    def test_skip_not_killable_even_when_base_bundle_batch_is_finished(self):
        """The base bundle protection covers *all* base batches.
        """
        self._create_batch(self.master_bundle, 'done', params=self.shared_params, build=None, is_last=False)

        build = self.Build.create({'params_id': self.shared_params.id, 'local_state': "testing", 'global_state': "testing"})
        batch = self._create_batch(self.dev_bundle, 'ready', params=self.shared_params, build=build)
        batch._skip()

        self.assertFalse(build.killable, 'build should not have been marked killable')

    def test_skip_does_not_touch_finished_builds(self):
        """Running and done builds are left alone."""
        for state in ('running', 'done'):
            with self.subTest(state=state):
                with self.env.cr.savepoint() as s:
                    build = self.Build.create({'params_id': self.other_params.id, 'local_state': state, 'global_state': state})
                    self.assertEqual(len(self.other_params.build_ids), 1)
                    batch = self._create_batch(self.dev_bundle, 'ready', params=self.other_params, build=build)
                    batch._skip()
                    self.assertNotKilled(build)
                    self.assertEqual(build.local_state, state)
                    s.rollback()

    # ------------------------------------------------------------------
    # edge cases
    # ------------------------------------------------------------------
    def test_skip_slot_without_build(self):
        """A slot whose build was never created is just flagged as skipped."""
        batch = self._create_batch(self.dev_bundle, 'ready', params=self.shared_params, build=None)
        batch._skip()
        self.assertEqual(batch.state, 'skipped')
        self.assertTrue(batch.slot_ids.skipped)

    def test_skip_is_a_noop_on_base_and_done_batches(self):
        """`_skip` never touches base bundles nor already done batches."""
        build = self.Build.create({'params_id': self.shared_params.id, 'local_state': "testing", 'global_state': "testing"})
        base_batch = self._create_batch(self.master_bundle, 'ready', params=self.shared_params, build=build)
        base_batch._skip()
        self.assertEqual(base_batch.state, 'ready')
        self.assertFalse(base_batch.slot_ids.skipped)
        self.assertNotKilled(build)

        done_batch_params = self.other_params
        done_batch_build = self.Build.create({'params_id': done_batch_params.id, 'local_state': "testing", 'global_state': "testing"})
        done_batch = self._create_batch(self.dev_bundle, 'done', params=done_batch_params, build=done_batch_build)
        done_batch._skip()
        self.assertEqual(done_batch.state, 'done')
        self.assertFalse(done_batch.slot_ids.skipped)
        self.assertNotKilled(done_batch_build)
