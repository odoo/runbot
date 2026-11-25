from datetime import datetime, timedelta

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import HttpCase, tagged

from .common import RunbotCase


class TestEvent(RunbotCase):

    def setUp(self):
        super().setUp()
        self.Event = self.env['runbot.event']
        self.version_master = self.dev_bundle.version_id
        self.now = fields.Datetime.now()

    def test_version_domain(self):
        all_versions = self.Event.create({'name': 'All', 'impact_errors': True})
        only_13 = self.Event.create({'name': '13', 'impact_errors': True, 'version_domain': "[('name', '=', '13.0')]"})
        self.assertTrue(all_versions._match_version(self.version_master))
        self.assertFalse(only_13._match_version(self.version_master))
        self.assertTrue(only_13._match_version(self.version_13))
        self.assertEqual((all_versions | only_13)._filter_versions(self.version_master), all_versions)
        self.assertEqual((all_versions | only_13)._filter_versions(self.version_master | self.version_13), all_versions | only_13)
        with self.assertRaises(ValidationError):
            self.Event.create({'name': 'Invalid', 'version_domain': "[('name', '='"})

    def test_get_events(self):
        stats = self.Event.create({'name': 'Stats', 'impact_stats': True, 'date': self.now - timedelta(days=2)})
        errors = self.Event.create({'name': 'Errors', 'impact_errors': True, 'date': self.now - timedelta(days=1)})
        old_errors = self.Event.create({'name': 'Old errors', 'impact_errors': True, 'date': self.now - timedelta(days=60)})
        self.assertEqual(self.Event._get_events('stats'), stats)
        self.assertEqual(self.Event._get_events('errors'), errors | old_errors)
        self.assertEqual(self.Event._get_events('errors', start=self.now - timedelta(days=30)), errors)
        self.assertEqual(self.Event._get_events('errors', end=self.now - timedelta(days=30)), old_errors)

    def test_error_events(self):
        build = self.Build.create({'params_id': self.base_params.id})
        error_content = self.env['runbot.build.error.content'].create({'content': 'foo'})
        self.env['runbot.build.error.link'].create({'build_id': build.id, 'error_content_id': error_content.id, 'log_date': self.now})
        error = error_content.error_id

        matching = self.Event.create({'name': 'Matching', 'impact_errors': True, 'date': self.now - timedelta(days=1)})
        self.Event.create({'name': 'Stats only', 'impact_stats': True, 'date': self.now - timedelta(days=1)})
        self.Event.create({'name': 'Other version', 'impact_errors': True, 'date': self.now - timedelta(days=1), 'version_domain': "[('name', '=', 'nonexisting')]"})
        self.Event.create({'name': 'Too old', 'impact_errors': True, 'date': self.now - timedelta(days=60)})
        self.assertEqual([event['id'] for event in error.history_data['events']], matching.ids)
        self.assertEqual(error.event_ids, matching)

    def test_events_before_batches(self):
        previous_batch, batch = self.dev_batch, self.Batch.create({'bundle_id': self.dev_bundle.id})
        (previous_batch | batch).state = 'done'
        self.env.cr.execute("UPDATE runbot_batch SET create_date = %s WHERE id = %s", (self.now - timedelta(days=1), previous_batch.id))
        self.env.cr.execute("UPDATE runbot_batch SET create_date = %s WHERE id = %s", (self.now, batch.id))
        (previous_batch | batch).invalidate_recordset(['create_date'])

        between = self.Event.create({'name': 'Between', 'impact_errors': True, 'date': self.now - timedelta(hours=12)})
        self.Event.create({'name': 'Before', 'impact_errors': True, 'date': self.now - timedelta(days=2)})
        self.Event.create({'name': 'Stats', 'impact_stats': True, 'date': self.now - timedelta(hours=12)})
        self.Event.create({'name': 'Other version', 'impact_errors': True, 'date': self.now - timedelta(hours=12), 'version_domain': "[('name', '=', '13.0')]"})
        error = self.env['runbot.build.error'].create({'name': 'foo'})
        self.assertEqual(error._get_events_before_batches(batch), between)
        self.assertFalse(error._get_events_before_batches(previous_batch))


@tagged('-at_install', 'post_install')
class TestEventApi(RunbotCase, HttpCase):

    def test_api_event(self):
        Event = self.env['runbot.event']
        now = datetime(2026, 11, 20, 12, 0, 0)
        errors = Event.create({'name': 'Errors', 'impact_errors': True, 'date': now, 'version_domain': "[('name', '=', '13.0')]"})
        stats = Event.create({'name': 'Stats', 'impact_stats': True, 'date': now - timedelta(days=2)})

        def get_ids(url):
            response = self.url_open(url)
            self.assertEqual(response.status_code, 200, response.text)
            return [event['id'] for event in response.json()]

        self.assertEqual(get_ids(f'/runbot/api/event/{errors.id},{stats.id}'), [errors.id, stats.id])
        self.assertEqual(get_ids('/runbot/api/event?impact=errors'), errors.ids)
        self.assertEqual(get_ids('/runbot/api/event?impact=stats'), stats.ids)
        self.assertEqual(get_ids('/runbot/api/event?version=master'), stats.ids)
        self.assertEqual(get_ids('/runbot/api/event?version=13.0'), [errors.id, stats.id])
        self.assertEqual(get_ids('/runbot/api/event?date_from=2026-11-19&date_to=2026-11-21'), errors.ids)
        self.assertEqual(get_ids('/runbot/api/event?limit=1'), errors.ids)
        event = self.url_open(f'/runbot/api/event/{errors.id}').json()[0]
        self.assertEqual(event['date'], '2026-11-20 12:00:00')
        self.assertEqual(event['version_domain'], "[('name', '=', '13.0')]")
