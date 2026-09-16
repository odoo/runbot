from types import SimpleNamespace
from unittest.mock import patch

from odoo.fields import Command
from odoo.tests.common import HttpCase, TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.runbot.controllers import frontend

CREATE_CONTEXT = {'no_reset_password': True, 'mail_create_nolog': True, 'mail_create_nosubscribe': True, 'mail_notrack': True}


@tagged('post_install', '-at_install')
class TestBuildErrorCounters(TransactionCase):
    """Tests of the navbar build error counters computed in the frontend controller."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.team = cls.env['runbot.team'].create({'name': 'Test team'})
        cls.other_team = cls.env['runbot.team'].create({'name': 'Other team'})
        cls.fixer = new_test_user(cls.env, login='fixer', name='fixer', groups='base.group_user', context=CREATE_CONTEXT)
        cls.fixer.runbot_team_ids = [Command.link(cls.team.id)]
        cls.teamless = new_test_user(cls.env, login='teamless', name='teamless', groups='base.group_user', context=CREATE_CONTEXT)

    def _create_error(self, name, **values):
        return self.env['runbot.build.error'].create(dict(values, name=name))

    def _counters(self, user):
        """Call the controller helper as if `user` was browsing the frontend."""
        with patch.object(frontend, 'request', SimpleNamespace(env=self.env(user=user))):
            return frontend._build_error_counters()

    def _total_errors(self):
        return self.env['runbot.build.error'].search_count([])

    def test_counters_public_user(self):
        """A public user should not see any counter, and no error should be counted for him"""
        self._create_error('An error for everybody')
        self._create_error('An error for the fixer', responsible=self.fixer.id)
        self._create_error('An error for the team', manual_team_id=self.team.id)

        counters = self._counters(self.env.ref('base.public_user'))

        self.assertEqual(counters, {'nb_build_errors': 0, 'nb_assigned_errors': 0, 'nb_team_errors': 0})

    def test_counters_no_assigned_error(self):
        """Without assigned error, only the generic counter is filled"""
        self._create_error('An error for everybody')
        self._create_error('An error for another user', responsible=self.teamless.id)
        self._create_error('An error for another team', manual_team_id=self.other_team.id)

        counters = self._counters(self.fixer)

        self.assertEqual(counters['nb_assigned_errors'], 0)
        self.assertEqual(counters['nb_team_errors'], 0)
        self.assertEqual(counters['nb_build_errors'], self._total_errors())

    def test_counters_assigned_errors(self):
        """Errors assigned to the user hide the generic counter"""
        self._create_error('An error for everybody')
        self._create_error('A first error for the fixer', responsible=self.fixer.id)
        self._create_error('A second error for the fixer', responsible=self.fixer.id)

        counters = self._counters(self.fixer)

        self.assertEqual(counters['nb_assigned_errors'], 2)
        self.assertEqual(counters['nb_team_errors'], 0)
        self.assertEqual(counters['nb_build_errors'], 0, "the generic counter is only a fallback")

    def test_counters_team_errors(self):
        """Only unassigned errors of the user teams are counted as team errors"""
        self._create_error('An error for everybody')
        self._create_error('An error for the team', manual_team_id=self.team.id)
        self._create_error('An assigned error of the team', manual_team_id=self.team.id, responsible=self.teamless.id)
        self._create_error('An error for another team', manual_team_id=self.other_team.id)

        counters = self._counters(self.fixer)

        self.assertEqual(counters['nb_assigned_errors'], 0)
        self.assertEqual(counters['nb_team_errors'], 1)
        self.assertEqual(counters['nb_build_errors'], 0, "the generic counter is only a fallback")

    def test_counters_assigned_and_team_errors(self):
        """Assigned and team errors are counted independently"""
        self._create_error('An error for everybody')
        self._create_error('An error for the fixer', responsible=self.fixer.id)
        self._create_error('A first error for the team', manual_team_id=self.team.id)
        self._create_error('A second error for the team', manual_team_id=self.team.id)

        counters = self._counters(self.fixer)

        self.assertEqual(counters['nb_assigned_errors'], 1)
        self.assertEqual(counters['nb_team_errors'], 2)
        self.assertEqual(counters['nb_build_errors'], 0, "the generic counter is only a fallback")

    def test_counters_user_without_team(self):
        """A user without team never gets team errors"""
        self._create_error('An error for everybody')
        self._create_error('An error for a team', manual_team_id=self.team.id)

        counters = self._counters(self.teamless)

        self.assertEqual(counters['nb_assigned_errors'], 0)
        self.assertEqual(counters['nb_team_errors'], 0)
        self.assertEqual(counters['nb_build_errors'], self._total_errors())


@tagged('post_install', '-at_install')
class TestBuildErrorLink(HttpCase):
    """Tests of the navbar build error link rendering"""

    def setUp(self):
        super().setUp()
        self.env['runbot.project'].create({'name': 'Frontend link tests'})
        with mute_logger('odoo.addons.base.models.ir_attachment'):
            self.fixer = new_test_user(
                self.env, login='fixer', name='fixer', password='fixerpwd',
                groups='base.group_user', context=CREATE_CONTEXT,
            )

    def test_error_link_hidden_for_public_user(self):
        self.env['runbot.build.error'].create({'name': 'An error for everybody'})

        response = self.url_open('/runbot')

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('/runbot/errors', response.text, "a public user should not see the build error link")

    def test_error_link_for_logged_user(self):
        self.env['runbot.build.error'].create({
            'name': 'An error for the fixer',
            'responsible': self.fixer.id,
        })
        self.authenticate('fixer', 'fixerpwd')

        response = self.url_open('/runbot')

        self.assertEqual(response.status_code, 200)
        self.assertIn('/runbot/errors', response.text)
        self.assertIn('You have 1 random bug assigned', response.text)
