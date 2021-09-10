from odoo.exceptions import AccessError
from odoo.tests.common import new_test_user

from .common import RunbotCase


class TestBatchLog(RunbotCase):

    def test_batch_log_write(self):
        """ test that a runbot manager can write a batch log """
        self.additionnal_setup()

        create_context = {'no_reset_password': True, 'mail_create_nolog': True, 'mail_create_nosubscribe': True, 'mail_notrack': True}
        simple_user = new_test_user(self.env, login='simple', name='simple', password='simple', context=create_context)
        runbot_admin = new_test_user(self.env, groups='runbot.group_runbot_admin,base.group_user', login='runbot_admin', name='runbot_admin', password='admin', context=create_context)

        # Ensure that a simple user cannot interfere in batch logs
        with self.assertRaises(AccessError):
            self.env['runbot.batch.log'].with_user(simple_user).create({
                'batch_id': self.branch_server.bundle_id.last_batch.id,
                'message': 'test_message',
                'level': 'INFO'
            })

        test_batch_log = self.env['runbot.batch.log'].with_user(runbot_admin).create({
                'batch_id': self.branch_server.bundle_id.last_batch.id,
                'message': 'test_message',
                'level': 'INFO'
            })

        self.assertEqual(test_batch_log.message, 'test_message')
