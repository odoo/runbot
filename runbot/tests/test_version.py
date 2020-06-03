# -*- coding: utf-8 -*-
from .common import RunbotCase


class TestVersion(RunbotCase):

    def test_basic_version(self):

        major_version = self.Version.create({'name': '12.0'})
        self.assertEqual(major_version.number, '12.00')
        self.assertTrue(major_version.is_major)

        saas_version = self.Version.create({'name': 'saas-12.1'})
        self.assertEqual(saas_version.number, '12.01')
        self.assertFalse(saas_version.is_major)

        self.assertGreater(saas_version.number, major_version.number)

        master_version = self.Version.create({'name': 'master'})
        self.assertEqual(master_version.number, '~')
        self.assertGreater(master_version.number, saas_version.number)

    def test_version_relations(self):
        version = self.env['runbot.version']
        v11 = version._get('11.0')
        v113 = version._get('saas-11.3')
        v12 = version._get('12.0')
        v122 = version._get('saas-12.2')
        v124 = version._get('saas-12.4')
        v13 = version._get('13.0')
        v131 = version._get('saas-13.1')
        v132 = version._get('saas-13.2')
        v133 = version._get('saas-13.3')
        master = version._get('master')

        self.assertEqual(v11.previous_major_version_id, version)
        self.assertEqual(v11.intermediate_version_ids, version)

        self.assertEqual(v113.previous_major_version_id, v11)
        self.assertEqual(v113.intermediate_version_ids, version)

        self.assertEqual(v12.previous_major_version_id, v11)
        self.assertEqual(v12.intermediate_version_ids, v113)

        self.assertEqual(v12.previous_major_version_id, v11)
        self.assertEqual(v12.intermediate_version_ids, v113)
        self.assertEqual(v12.next_major_version_id, v13)
        self.assertEqual(v12.next_intermediate_version_ids, v124 | v122)

        self.assertEqual(v13.previous_major_version_id, v12)
        self.assertEqual(v13.intermediate_version_ids, v124 | v122)
        self.assertEqual(v13.next_major_version_id, master)
        self.assertEqual(v13.next_intermediate_version_ids, v133 | v132 | v131)

        self.assertEqual(v132.previous_major_version_id, v13)
        self.assertEqual(v132.intermediate_version_ids, v131)
        self.assertEqual(v132.next_major_version_id, master)
        self.assertEqual(v132.next_intermediate_version_ids, v133)

        self.assertEqual(master.previous_major_version_id, v13)
        self.assertEqual(master.intermediate_version_ids, v133 | v132 | v131)
