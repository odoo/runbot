# -*- coding: utf-8 -*-
from odoo.tests import common
from odoo.exceptions import UserError

from odoo.addons.runbot.models.res_config_settings import RE_POSTGRE_URI


class TestConfigSettings(common.TransactionCase):

    def tests_log_user_validation(self):
        """ Test the validation of the logdb_uri """

        simple_uri = 'postgresql://mr_nobody:aS3cr3T!@ahost'

        # test regex
        res = RE_POSTGRE_URI.search(simple_uri)
        self.assertTrue(res)
        self.assertEqual('postgresql', res.group('protocol'))
        self.assertEqual('mr_nobody', res.group('user'))
        self.assertEqual('aS3cr3T!', res.group('password'))

        rcs = self.env['res.config.settings'].create({'runbot_logdb_uri': 'blah blah'})

        with self.assertRaises(UserError):
            rcs._grant_access()

        # check empty password or no password
        rcs.write({'runbot_logdb_uri': 'postgresql://mr_nobody:@ahost'})
        with self.assertRaises(UserError):
            rcs._grant_access()

        rcs.write({'runbot_logdb_uri': 'postgresql://mr_nobody@ahost'})
        with self.assertRaises(UserError):
            rcs._grant_access()

        # check a valid URI twice to be sure that it works even if the user already exists
        rcs.write({'runbot_logdb_uri': simple_uri})
        rcs._grant_access()
        rcs._grant_access()
