from odoo.fields import Field
from collections.abc import MutableMapping
from psycopg2.extras import Json


class JsonDictField(Field):
    type = 'jsonb'
    column_type = ('jsonb', 'jsonb')
    column_cast_from = ('varchar',)

    def convert_to_write(self, value, record):
        return value

    def convert_to_column(self, value, record, values=None, validate=True):
        val = self.convert_to_cache(value, record, validate=validate)
        return Json(val) if val else None

    def convert_to_cache(self, value, record, validate=True):
        return value.dict if isinstance(value, FieldDict) else value if isinstance(value, dict) else None

    def convert_to_record(self, value, record):
        return FieldDict(value or {}, self, record)

    def convert_to_read(self, value, record, use_name_get=True):
        return self.convert_to_cache(value, record)


class FieldDict(MutableMapping):

    def __init__(self, init_dict, field, record):
        self.field = field
        self.record = record
        self.dict = init_dict

    def __setitem__(self, key, value):
        new = self.dict.copy()
        new[key] = value
        self.record[self.field.name] = new

    def __getitem__(self, key):
        return self.dict[key]

    def __delitem__(self, key):
        new = self.dict.copy()
        del new[key]
        self.record[self.field.name] = new

    def __iter__(self):
        return iter(self.dict)

    def __len__(self):
        return len(self.dict)
