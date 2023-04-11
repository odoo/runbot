from odoo import api, SUPERUSER_ID
from odoo.tests import TransactionCase
from unittest.mock import patch


class TestConcurrency(TransactionCase):
    def test_local_status_update(self):
        """
        This test ensures that a parent build global state will eventually be updated
        even if updated concurrenctly in 2 different transactions, without transaction error
        """
        with self.registry.cursor() as cr0:
            env0 = api.Environment(cr0, SUPERUSER_ID, {})
            host = env0.ref('runbot_populate.main_host')
            host._process_messages()  # ensure queue is empty
            parent_build = env0.ref('runbot_populate.build_base')
            build_child1 = env0.ref('runbot_populate.build_child1')
            build_child2 = env0.ref('runbot_populate.build_child2')
            parent_build.local_state = 'done'
            self.assertEqual(host.host_message_ids.mapped('message'), [])
            build_child1.local_state = 'testing'
            build_child2.local_state = 'testing'
            self.assertEqual(host.host_message_ids.mapped('message'), ['global_updated', 'global_updated'])
            self.assertEqual(host.host_message_ids.build_id, parent_build)
            host._process_messages()
            self.assertEqual(parent_build.global_state, 'waiting')
            env0.cr.commit()  # youplahé

            with self.registry.cursor() as cr1:
                env1 = api.Environment(cr1, SUPERUSER_ID, {})
                with self.registry.cursor() as cr2:
                    env2 = api.Environment(cr2, SUPERUSER_ID, {})
                    build_child_cr1 = env1['runbot.build'].browse(build_child1.id)
                    build_child_cr2 = env2['runbot.build'].browse(build_child2.id)
                    self.assertEqual(build_child_cr1.parent_id.global_state, 'waiting')
                    self.assertEqual(build_child_cr1.parent_id.children_ids.mapped('local_state'), ['testing', 'testing'])
                    self.assertEqual(build_child_cr2.parent_id.global_state, 'waiting')
                    self.assertEqual(build_child_cr2.parent_id.children_ids.mapped('local_state'), ['testing', 'testing'])
                    build_child_cr1.local_state = 'done'
                    build_child_cr2.local_state = 'done'
                    # from the point of view of each transaction, the other one local_state didn't changed
                    self.assertEqual(build_child_cr1.parent_id.children_ids.mapped('local_state'), ['testing', 'done'])
                    self.assertEqual(build_child_cr2.parent_id.global_state, 'waiting')
                    self.assertEqual(build_child_cr2.parent_id.children_ids.mapped('local_state'), ['done', 'testing'])
                    env1.cr.commit()
                    env2.cr.commit()
                    env0.cr.commit()  # not usefull just to ensure we have efefct of other transactions
                    env0.cache.invalidate()
                    self.assertEqual(parent_build.children_ids.mapped('local_state'), ['done', 'done'])
                    self.assertEqual(parent_build.children_ids.mapped('global_state'), ['done', 'done'])
                    self.assertEqual(host.host_message_ids.mapped('message'), ['global_updated', 'global_updated'])
                    self.assertEqual(host.host_message_ids.build_id, parent_build)
                    # at this point, this assertion is true, but not expected : self.assertEqual(parent_build.global_state, 'waiting')
                    host._process_messages()
                    self.assertEqual(parent_build.global_state, 'done')
