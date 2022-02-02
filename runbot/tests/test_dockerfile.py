# -*- coding: utf-8 -*-
import logging

from unittest.mock import patch, mock_open

from odoo.tests.common import Form, tagged, HttpCase
from .common import RunbotCase

_logger = logging.getLogger(__name__)


@tagged('-at_install', 'post_install')
class TestDockerfile(RunbotCase, HttpCase):

    def test_dockerfile_base_fields(self):
        xml_content = """<t t-call="runbot.docker_base">
    <t t-set="custom_values" t-value="{
      'from': 'ubuntu:focal',
      'phantom': True,
      'additional_pip': 'babel==2.8.0',
      'chrome_source': 'odoo',
      'chrome_version': '86.0.4240.183-1',
    }"/>
</t>
"""

        focal_template = self.env['ir.ui.view'].create({
            'name': 'docker_focal_test',
            'type': 'qweb',
            'key': 'docker.docker_focal_test',
            'arch_db': xml_content
        })

        dockerfile = self.env['runbot.dockerfile'].create({
            'name': 'Tests Ubuntu Focal (20.0)[Chrome 86]',
            'template_id': focal_template.id,
            'to_build': True
        })

        self.assertEqual(dockerfile.image_tag, 'odoo:TestsUbuntuFocal20.0Chrome86')
        self.assertTrue(dockerfile.dockerfile.startswith('FROM ubuntu:focal'))
        self.assertIn(' apt-get install -y -qq google-chrome-stable=86.0.4240.183-1', dockerfile.dockerfile)
        self.assertIn('# Install phantomjs', dockerfile.dockerfile)
        self.assertIn('pip install --no-cache-dir babel==2.8.0', dockerfile.dockerfile)

        # test view update
        xml_content = xml_content.replace('86.0.4240.183-1', '87.0-1')
        dockerfile_form = Form(dockerfile)
        dockerfile_form.arch_base = xml_content
        dockerfile_form.save()

        self.assertIn('apt-get install -y -qq google-chrome-stable=87.0-1', dockerfile.dockerfile)

        # Ensure that only the test dockerfile will be found by docker_run
        self.env['runbot.dockerfile'].search([('id', '!=', dockerfile.id)]).update({'to_build': False})

        def write_side_effect(content):
            self.assertIn('apt-get install -y -qq google-chrome-stable=87.0-1', content)

        docker_build_mock = self.patchers['docker_build']
        docker_build_mock.return_value = (True, None)
        mopen = mock_open()
        rb_host = self.env['runbot.host'].create({'name': 'runbotxxx.odoo.com'})
        with patch('builtins.open', mopen) as file_mock:
            file_handle_mock = file_mock.return_value.__enter__.return_value
            file_handle_mock.write.side_effect = write_side_effect
            rb_host._docker_build()
