from .common import RunbotCase


class TestLinkedBuildGc(RunbotCase):

    def setUp(self):
        super().setUp()
        self.BuildLink = self.env['runbot.build.link']
        self.dev_batch.state = 'done'
        self.host = self.env['runbot.host'].create({'name': 'runbot_link', 'nb_worker': 1})

        # a pending build waiting for a slot, otherwise the gc returns early
        self.Build.create({'params_id': self.base_params.id, 'local_state': 'pending'})

        self.parent = self.create_new_parent(self.dev_batch)
        self.child_params = self.BuildParameters.create({
            'version_id': self.version_13.id,
            'project_id': self.project.id,
            'config_id': self.default_config.id,
            'create_batch_id': self.dev_batch.id,
            'extra_params': 'child',
        })

        self.child = self.Build.create({
            'params_id': self.child_params.id,
            'local_state': 'testing',
            'host': self.host.name,
        })
        self.link = self.BuildLink.create({'parent_id': self.parent.id, 'child_id': self.child.id, 'link_type': 'created'})
        self.trigger_server.batch_dependent = True  # avoid linking of top_level parent as upgrade trigger is configured

    def create_new_parent(self, batch=None, local_state='done', killable=False):
        if not batch:
            batch = self.dev_bundle._force()
            batch.state = 'ready'
        params = self.BuildParameters.create({
            'version_id': self.version_13.id,
            'project_id': self.project.id,
            'config_id': self.default_config.id,
            'trigger_id': self.trigger_server.id,
            'create_batch_id': batch.id,
        })
        build = self.Build.create({
            'params_id': params.id,
            'local_state': local_state,
            'killable': killable,
            'create_batch_id': batch.id,
        })
        self.env['runbot.batch.slot'].create({
            'batch_id': batch.id,
            'trigger_id': self.trigger_server.id,
            'build_id': build.id,
            'params_id': params.id,
            'link_type': 'created',
        })
        return build

    def test_living_parent(self):
        self.env['runbot.runbot']._gc_testing(self.host)
        self.assertFalse(self.child.to_kill)

    def test_killable_parent(self):
        self.parent.killable = True
        self.env['runbot.runbot']._gc_testing(self.host)
        self.assertTrue(self.child.to_kill)

    def test_orphan_link(self):
        self.link.orphan_result = True
        self.env['runbot.runbot']._gc_testing(self.host)
        self.assertTrue(self.child.to_kill)

    def test_second_living_parent(self):
        self.parent.killable = True
        other_parent = self.create_new_parent()
        self.BuildLink.create({'parent_id': other_parent.id, 'child_id': self.child.id})
        self.env['runbot.runbot']._gc_testing(self.host)
        self.assertFalse(self.child.to_kill)

    def test_new_batch_window(self):
        self.parent.killable = True
        batch = self.dev_bundle._force()
        self.assertEqual(batch.state, 'preparing')
        self.env['runbot.runbot']._gc_testing(self.host)
        self.assertFalse(self.child.to_kill, 'a preparing batch may create a new parent')

        batch.state = 'ready'
        new_parent = self.create_new_parent(batch, local_state='testing')
        self.env['runbot.runbot']._gc_testing(self.host)
        self.assertFalse(self.child.to_kill, 'the new parent may still link this child since the build is testing')

        new_parent.local_state = 'done'
        self.env['runbot.runbot']._gc_testing(self.host)
        self.assertTrue(self.child.to_kill)


class TestLinkedBuildGlobals(RunbotCase):
    """global_state and global_result of a parent using build links"""

    def setUp(self):
        super().setUp()
        self.BuildLink = self.env['runbot.build.link']
        self.parent = self.create_build('parent')
        self.parent.local_state = 'done'

    def create_build(self, name, **values):
        params = self.BuildParameters.create({
            'version_id': self.version_13.id,
            'project_id': self.project.id,
            'config_id': self.default_config.id,
            'create_batch_id': self.dev_batch.id,
            'extra_params': name,
        })
        return self.Build.create(dict(values, params_id=params.id))

    def link_child(self, name, parent=None, orphan_result=False):
        child = self.create_build(name)
        self.BuildLink.create({
            'parent_id': (parent or self.parent).id,
            'child_id': child.id,
            'orphan_result': orphan_result,
            'link_type': 'created',
        })
        return child

    def test_no_child(self):
        self.assertEqual(self.parent.global_state, 'done')
        self.assertEqual(self.parent.global_result, 'ok')

    def test_pending_child(self):
        self.link_child('child')
        self.parent._update_globals()
        self.assertEqual(self.parent.global_state, 'waiting')
        self.assertEqual(self.parent.global_result, 'ok')

    def test_done_child(self):
        child = self.link_child('child')
        child.local_state = 'done'
        self.parent._update_globals()
        self.assertEqual(self.parent.global_state, 'done')
        self.assertEqual(self.parent.global_result, 'ok')

    def test_ko_child(self):
        child = self.link_child('child')
        child.write({'local_state': 'done', 'local_result': 'ko'})
        self.parent._update_globals()
        self.assertEqual(self.parent.global_state, 'done')
        self.assertEqual(self.parent.global_result, 'ko')

    def test_killed_child(self):
        child = self.link_child('child')
        child.write({'local_state': 'done', 'local_result': 'killed'})
        self.parent._update_globals()
        self.assertEqual(self.parent.global_state, 'done')
        self.assertEqual(self.parent.global_result, 'ko', 'a killed child is capped to ko')

    def test_orphan_child(self):
        child = self.link_child('child', orphan_result=True)
        child.write({'local_state': 'done', 'local_result': 'ko'})
        self.parent._update_globals()
        self.assertEqual(self.parent.global_state, 'done')
        self.assertEqual(self.parent.global_result, 'ok')

    def test_orphan_child_does_not_hold_the_parent(self):
        self.link_child('child', orphan_result=True)
        self.parent._update_globals()
        self.assertEqual(self.parent.global_state, 'done')

    def test_worst_result_of_children(self):
        warn_child = self.link_child('warn_child')
        ko_child = self.link_child('ko_child')
        warn_child.write({'local_state': 'done', 'local_result': 'warn'})
        ko_child.write({'local_state': 'done', 'local_result': 'ko'})
        self.parent._update_globals()
        self.assertEqual(self.parent.global_result, 'ko')

    def test_one_pending_child_keeps_waiting(self):
        done_child = self.link_child('done_child')
        self.link_child('pending_child')
        done_child.write({'local_state': 'done', 'local_result': 'ko'})
        self.parent._update_globals()
        self.assertEqual(self.parent.global_state, 'waiting')
        self.assertEqual(self.parent.global_result, 'ko')

    def test_local_result_of_the_parent(self):
        child = self.link_child('child')
        child.local_state = 'done'
        self.parent.local_result = 'ko'
        self.parent._update_globals()
        self.assertEqual(self.parent.global_result, 'ko')

    def test_shared_child(self):
        other_parent = self.create_build('other_parent')
        other_parent.local_state = 'done'
        child = self.link_child('child')
        self.BuildLink.create({'parent_id': other_parent.id, 'child_id': child.id})
        child.write({'local_state': 'done', 'local_result': 'ko'})
        (self.parent + other_parent)._update_globals()
        self.assertEqual(self.parent.global_result, 'ko')
        self.assertEqual(other_parent.global_result, 'ko')

    def test_shared_child_orphan_on_one_parent_only(self):
        other_parent = self.create_build('other_parent')
        other_parent.local_state = 'done'
        child = self.link_child('child')
        self.BuildLink.create({
            'parent_id': other_parent.id,
            'child_id': child.id,
            'orphan_result': True,
        })
        child.write({'local_state': 'done', 'local_result': 'ko'})
        (self.parent + other_parent)._update_globals()
        self.assertEqual(self.parent.global_result, 'ko')
        self.assertEqual(other_parent.global_result, 'ok')

    def test_parent_id_child(self):
        child = self.create_build('child', parent_id=self.parent.id)
        self.assertEqual(self.parent.global_state, 'waiting')
        child.write({'local_state': 'done', 'local_result': 'ko'})
        self.parent._update_globals()
        self.assertEqual(self.parent.global_state, 'done')
        self.assertEqual(self.parent.global_result, 'ko')

    def test_new_link_updates_the_parent(self):
        child = self.link_child('child')
        child.local_state = 'done'
        self.parent._update_globals()
        self.assertEqual(self.parent.global_state, 'done')

        self.link_child('new_child')
        self.assertEqual(self.parent.global_state, 'waiting', 'creating a link must update the parent')

    def test_orphaning_a_link_updates_the_parent(self):
        child = self.link_child('child')
        child.write({'local_state': 'done', 'local_result': 'ko'})
        self.parent._update_globals()
        self.assertEqual(self.parent.global_result, 'ko')

        self.parent.child_link_ids.orphan_result = True
        self.assertEqual(self.parent.global_result, 'ok', 'orphaning a link must update the parent')


class TestLinkCandidate(RunbotCase):
    """_get_link_candidate: which existing build may be reused for a new child"""

    def setUp(self):
        super().setUp()
        self.child_params = self.create_params('child')
        self.parent = self.create_build('parent')

    def create_params(self, name):
        return self.BuildParameters.create({
            'version_id': self.version_13.id,
            'project_id': self.project.id,
            'config_id': self.default_config.id,
            'create_batch_id': self.dev_batch.id,
            'extra_params': name,
        })

    def create_build(self, name, **values):
        return self.Build.create(dict(values, params_id=self.create_params(name).id))

    def create_candidate(self, **values):
        return self.Build.create(dict(values, params_id=self.child_params.id))

    def link(self, parent, child, orphan_result=False):
        return self.env['runbot.build.link'].create({
            'parent_id': parent.id,
            'child_id': child.id,
            'orphan_result': orphan_result,
        })

    def candidate(self, orphan=False):
        return self.parent._get_link_candidate(self.child_params, orphan=orphan)

    def test_no_candidate(self):
        self.assertFalse(self.candidate())

    def test_pending_candidate(self):
        build = self.create_candidate()
        self.assertEqual(self.candidate(), build)

    def test_prefer_done_ok(self):
        done_build = self.create_candidate()
        done_build.local_state = 'done'
        self.create_candidate()
        self.assertEqual(self.candidate(), done_build, 'a finished green build answers immediately')

    def test_prefer_pending_over_red(self):
        red_build = self.create_candidate()
        red_build.write({'local_state': 'done', 'local_result': 'ko'})
        pending_build = self.create_candidate()
        self.assertEqual(self.candidate(), pending_build)

    def test_red_as_last_resort(self):
        red_build = self.create_candidate()
        red_build.write({'local_state': 'done', 'local_result': 'ko'})
        self.assertEqual(self.candidate(), red_build)

    def test_ignore_self(self):
        parent = self.Build.create({'params_id': self.child_params.id})
        self.assertFalse(parent._get_link_candidate(self.child_params))

    def test_ignore_parent_id(self):
        self.create_candidate(parent_id=self.parent.id)
        self.assertFalse(self.candidate())

    def test_ignore_killed(self):
        build = self.create_candidate()
        build.write({'local_state': 'done', 'local_result': 'killed'})
        self.assertFalse(self.candidate())

    def test_ignore_skipped(self):
        build = self.create_candidate()
        build.write({'local_state': 'done', 'local_result': 'skipped'})
        self.assertFalse(self.candidate())

    def test_ignore_all_links_orphaned(self):
        build = self.create_candidate()
        self.link(self.parent, build, orphan_result=True)
        self.assertFalse(self.candidate())

    def test_keep_one_living_link(self):
        build = self.create_candidate()
        other_parent = self.create_build('other_parent')
        self.link(self.parent, build, orphan_result=True)
        self.link(other_parent, build)
        self.assertEqual(self.candidate(), build)

    def test_normal_request_ignores_orphan_build(self):
        self.create_candidate(orphan_result=True)
        self.assertFalse(self.candidate())

    def test_orphan_request_ignores_normal_build(self):
        self.create_candidate()
        self.assertFalse(self.candidate(orphan=True))

    def test_orphan_request_finds_orphan_build(self):
        build = self.create_candidate(orphan_result=True)
        self.assertEqual(self.candidate(orphan=True), build)

    def test_keep_host(self):
        self.parent.write({'keep_host': True, 'host': 'host_a'})
        self.create_candidate(host='host_b')
        other_host_build = self.create_candidate(host='host_a')
        self.assertEqual(self.candidate(), other_host_build)

    def test_any_host_without_keep_host(self):
        build = self.create_candidate(host='host_b')
        self.assertEqual(self.candidate(), build)

    def test_staging_takes_green(self):
        self.dev_bundle.is_staging = True
        build = self.create_candidate()
        build.local_state = 'done'
        self.assertEqual(self.candidate(), build)

    def test_staging_ignores_red(self):
        self.dev_bundle.is_staging = True
        build = self.create_candidate()
        build.write({'local_state': 'done', 'local_result': 'ko'})
        self.assertFalse(self.candidate(), 'a merge decision cannot inherit a red result')

    def test_staging_takes_unfinished(self):
        self.dev_bundle.is_staging = True
        build = self.create_candidate()
        self.assertEqual(self.candidate(), build, 'sharing a build that is not red yet is fine')

    def test_staging_ignores_warn(self):
        self.dev_bundle.is_staging = True
        build = self.create_candidate()
        build.write({'local_state': 'done', 'local_result': 'warn'})
        self.assertFalse(self.candidate())

    def test_warn_as_last_resort(self):
        build = self.create_candidate()
        build.write({'local_state': 'done', 'local_result': 'warn'})
        self.assertEqual(self.candidate(), build)
