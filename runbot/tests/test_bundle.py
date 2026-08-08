from .common import RunbotCase

class TestBundleCreation(RunbotCase):
    def test_version_at_bundle_creation(self):
        saas_name = 'saas-27.2'
        saas_bundle = self.Bundle.create({
            'name': saas_name,
            'project_id': self.project.id
        })
        saas_bundle.is_base = True
        self.assertEqual(saas_bundle.version_id.name, saas_name, 'The bundle version_id should create base version')

        dev_name = 'saas-27.2-brol-bro'
        dev_bundle = self.Bundle.create({
            'name': dev_name,
            'project_id': self.project.id
        })
        self.assertEqual(dev_bundle.version_id.name, saas_name)

        self.assertFalse(self.Version.search([('name', '=', dev_name)]), 'A dev bundle should not summon a new version')
        dev_name = 'saas-27.2-brol-zzz'
        dev_bundle = self.Bundle.create({
            'name': dev_name,
            'project_id': self.project.id,
            'is_base': True
        })
        self.assertFalse(self.Version.search([('name', '=', dev_name)]), 'A dev bundle should not summon a new version, even if is_base is True')
        self.assertEqual(dev_bundle.version_id.id, False)
