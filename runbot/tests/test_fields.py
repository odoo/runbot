from odoo.tests.common import BaseCase
from types import SimpleNamespace

from ..fields import FieldDict
from .common import RunbotCase


class TestFieldDict(BaseCase):

    def test_update_multiple_keys(self):
        record = {'config_data': {'existing': 0}}
        values = FieldDict(record['config_data'], SimpleNamespace(name='config_data'), record)
        values.update({'first': 1, 'second': 2})
        self.assertEqual(record['config_data'], {'existing': 0, 'first': 1, 'second': 2})
        self.assertEqual(dict(values), record['config_data'])

    def test_delete_and_set(self):
        record = {'config_data': {'first': 1, 'second': 2}}
        values = FieldDict(record['config_data'], SimpleNamespace(name='config_data'), record)
        del values['first']
        values['third'] = 3
        self.assertEqual(record['config_data'], {'second': 2, 'third': 3})
        self.assertEqual(dict(values), record['config_data'])

    def test_failed_write_preserves_snapshot(self):
        class RejectWrite(dict):
            def __setitem__(self, key, value):
                raise ValueError('read only')

        record = RejectWrite(config_data={'first': 1})
        values = FieldDict(record['config_data'], SimpleNamespace(name='config_data'), record)
        with self.assertRaises(ValueError):
            values['first'] = 2
        self.assertEqual(dict(values), {'first': 1})


class TestJsonDictField(RunbotCase):

    def test_mapping_mutations(self):
        self.trigger_server.config_data = {'existing': 0}
        values = self.trigger_server.config_data
        values.update({'first': 1, 'second': 2})
        del values['existing']
        self.assertEqual(dict(values), {'first': 1, 'second': 2})
        self.assertEqual(dict(self.trigger_server.config_data), dict(values))
