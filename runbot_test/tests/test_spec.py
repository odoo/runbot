from odoo import fields
from odoo.tests.common import TransactionCase, tagged, new_test_user


@tagged('-at_install', 'post_install')
class TestSpec(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.Parent = cls.env['runbot.test.model.parent']
        cls.Child = cls.env['runbot.test.model.child']
        cls.user_basic = new_test_user(
            cls.env, 'basic',
        )

    def test_parent_spec(self):
        spec = self.Parent._api_public_specification()
        self.assertNotIn('private_field', spec)

        self.assertEqual(
            spec['field_bool'],
            {
                '__type': 'boolean',
            }
        )

        self.assertIn('field_many2one', spec)
        sub_spec = spec['field_many2one']['fields']
        self.assertIn('field_many2many', spec)
        self.assertIn('field_many2many', sub_spec)

        self.assertTrue(self.Parent._api_verify_specification(spec))

    def test_child_spec(self):
        spec = self.Child._api_public_specification()

        self.assertIn('parent_id', spec)
        self.assertIn('data', spec)

        sub_spec = spec['parent_id']
        # The reverse relation should not be part of the sub spec, as we already
        # traversed that relation
        self.assertNotIn('field_one2many', sub_spec)

        self.assertTrue(self.Child._api_verify_specification(spec))

    def test_parent_read_basic(self):
        today = fields.Date.today()
        now = fields.Datetime.now()
        parent = self.Parent.create({
            'field_bool': True,
            'field_date': today,
            'field_datetime': now,
            'field_json': {'test': 1}
        })
        self.assertEqual(
            parent._api_read({'field_bool': {}, 'field_date': {}, 'field_datetime': {}, 'field_json': {}}),
            [{
                'id': parent.id,
                'field_bool': True,
                'field_date': today,
                'field_datetime': now,
                'field_json': {'test': 1}
            }]
        )

    def test_parent_read_m2o(self):
        parent_1 = self.Parent.create({'field_integer': 1})
        parent_2 = self.Parent.create({'field_integer': 2, 'field_many2one': parent_1.id})

        # Read without sub spec
        self.assertEqual(
            parent_1._api_read({'field_many2one': {}}),
            [{'id': parent_1.id, 'field_many2one': False}]
        )
        self.assertEqual(
            parent_2._api_read({'field_many2one': {}}),
            [{'id': parent_2.id, 'field_many2one': parent_1.id}]
        )
        # Read with sub spec
        self.assertEqual(
            parent_1._api_read({'field_many2one': {'fields': {'field_integer': {}}}}),
            [{'id': parent_1.id, 'field_many2one': False}]
        )
        self.assertEqual(
            parent_2._api_read({'field_many2one': {'fields': {'field_integer': {}}}}),
            [{'id': parent_2.id, 'field_many2one': {'id': parent_1.id, 'field_integer': parent_1.field_integer}}]
        )
        both = parent_1 | parent_2
        # Read both with sub spec
        self.assertEqual(
            both._api_read({'field_integer': {}, 'field_many2one': {'fields': {'field_integer': {}}}}),
            [
                {'id': parent_1.id, 'field_integer': parent_1.field_integer, 'field_many2one': False},
                {'id': parent_2.id, 'field_integer': parent_2.field_integer, 'field_many2one': {
                    'id': parent_1.id, 'field_integer': parent_1.field_integer,
                }},
            ]
        )

    def test_parent_read_one2many(self):
        parent = self.Parent.create({
            'field_float': 13.37,
            'field_one2many': [
                (0, 0, {'data': 1}),
                (0, 0, {'data': 2}),
                (0, 0, {'data': 3}),
            ]
        })
        parent_no_o2m = self.Parent.create({
            'field_float': 13.37,
        })

        # Basic read
        self.assertEqual(
            parent._api_read({
                'field_float': {},
                'field_one2many': {
                    'fields': {
                        'data': {}
                    },
                }
            }),
            [{
                'id': parent.id,
                'field_float': 13.37,
                'field_one2many': [
                    {'id': parent.field_one2many[0].id, 'data': 1},
                    {'id': parent.field_one2many[1].id, 'data': 2},
                    {'id': parent.field_one2many[2].id, 'data': 3},
                ]
            }]
        )
        self.assertEqual(
            parent_no_o2m._api_read({
                'field_float': {},
                'field_one2many': {
                    'fields': {
                        'data': {},
                    }
                },
            }),
            [{
                'id': parent_no_o2m.id,
                'field_float': 13.37,
                'field_one2many': [],
            }]
        )

        # Reading parent_id through field_one2many_computed is allowed since relationship is not known
        self.env.invalidate_all()
        spec = {
            'field_float': {},
            'field_one2many': {
                'fields': {
                    'data': {},
                    'parent_id': {
                        'fields': {
                            'field_float': {},
                        }
                    },
                }
            },
        }
        self.assertEqual(
            parent._api_read(spec),
            [{
                'id': parent.id,
                'field_float': 13.37,
                'field_one2many': [
                    {'id': parent.field_one2many[0].id, 'data': 1, 'parent_id': {'id': parent.id, 'field_float': 13.37}},
                    {'id': parent.field_one2many[1].id, 'data': 2, 'parent_id': {'id': parent.id, 'field_float': 13.37}},
                    {'id': parent.field_one2many[2].id, 'data': 3, 'parent_id': {'id': parent.id, 'field_float': 13.37}},
                ]
            }]
        )
        # It should not work with basic user
        self.assertFalse(parent.with_user(self.user_basic)._api_verify_specification(spec))
        # It should however work with the computed version
        spec['field_one2many_computed'] = spec['field_one2many']
        spec.pop('field_one2many')
        self.assertEqual(
            parent.with_user(self.user_basic)._api_read(spec),
            [{
                'id': parent.id,
                'field_float': 13.37,
                'field_one2many_computed': [
                    {'id': parent.field_one2many[0].id, 'data': 1, 'parent_id': {'id': parent.id, 'field_float': 13.37}},
                    {'id': parent.field_one2many[1].id, 'data': 2, 'parent_id': {'id': parent.id, 'field_float': 13.37}},
                    {'id': parent.field_one2many[2].id, 'data': 3, 'parent_id': {'id': parent.id, 'field_float': 13.37}},
                ]
            }]
        )

    def test_parent_read_one2many_private(self):
        # Check the private one2many (field is public, model is not), we can read id but nothing else
        parent = self.Parent.create({
            'field_float': 13.37,
            'field_one2many_private': [
                (0, 0, {'data': 1}),
            ]
        }).with_user(self.user_basic)
        spec = parent._api_public_specification()
        self.assertNotIn('fields', spec['field_one2many_private'])
        result = parent._api_read(spec)
        self.assertEqual(
            result[0]['field_one2many_private'],
            [parent.field_one2many_private.id],
        )
        with self.assertRaises(ValueError):
            parent._api_verify_specification({
                'field_one2many_private': {
                    'fields': {
                        'data': {}
                    }
                }
            })


    def test_parent_read_m2m(self):
        parent = self.Parent.create({
            'field_integer': 1,
            'field_many2many': [
                (0, 0, {'field_integer': 2}),
            ],
        })
        other_parent = parent.field_many2many
        self.assertEqual(
            parent._api_read({'field_integer': {}, 'field_many2many': {}}),
            [
                {'id': parent.id, 'field_many2many': list(parent.field_many2many.ids), 'field_integer': 1},
            ],
        )
        self.env.invalidate_all()
        spec = {
            'field_many2many_computed': {
                'fields': {
                    'field_many2many_computed': {
                        'fields': {
                            'field_integer': {}
                        }
                    }
                }
            }
        }
        self.assertEqual(
            parent._api_read(spec),
            [
                {'id': parent.id, 'field_many2many_computed': [
                    {'id': other_parent.id, 'field_many2many_computed': [
                        {'id': parent.id, 'field_integer': 1}
                    ]}
                ]}
            ]
        )
        self.assertFalse(parent.with_user(self.user_basic)._api_verify_specification(spec))

    def test_parent_read_cyclic_parent(self):
        parent_1 = self.Parent.create({
            'field_integer': 1,
        })
        parent_2 = self.Parent.create({
            'field_integer': 2,
            'field_many2one': parent_1.id,
        })
        parent_1.field_many2one = parent_2
        self.env.invalidate_all()
        both = (parent_1 | parent_2)
        self.assertEqual(
            both._api_read({'field_many2one': {}}),
            [
                {'id': parent_1.id, 'field_many2one': parent_2.id},
                {'id': parent_2.id, 'field_many2one': parent_1.id},
            ]
        )
        self.assertEqual(
            both.with_user(self.user_basic)._api_read({'field_many2one': {}}),
            [
                {'id': parent_1.id, 'field_many2one': parent_2.id},
                {'id': parent_2.id, 'field_many2one': parent_1.id},
            ]
        )
        spec = {'field_many2one': {'fields': {'field_many2one': {}}}}
        self.assertEqual(
            both._api_read(spec),
            [
                {'id': parent_1.id, 'field_many2one': {'id': parent_2.id, 'field_many2one': parent_1.id}},
                {'id': parent_2.id, 'field_many2one': {'id': parent_1.id, 'field_many2one': parent_2.id}},
            ]
        )
        self.assertFalse(both.with_user(self.user_basic)._api_verify_specification(spec))

            
