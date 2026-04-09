# -*- coding: utf-8 -*-
import getpass
import logging
import os
import re
import time
from psycopg2.errors import UniqueViolation
from requests.exceptions import HTTPError

from odoo import Command, exceptions
from unittest.mock import patch, mock_open, MagicMock

from odoo.tests.common import tagged, HttpCase, mute_logger
from .common import RunbotCase

_logger = logging.getLogger(__name__)

USERUID = os.getuid()
USERGID = os.getgid()
USERNAME = getpass.getuser()

@tagged('-at_install', 'post_install')
class TestDockerfile(RunbotCase, HttpCase):

    def test_docker_default(self):
        self.maxDiff = None

        with (
            patch('odoo.addons.runbot.models.docker.USERNAME', 'TestUser'),
            patch('odoo.addons.runbot.models.docker.USERUID', '4242'),
            patch('odoo.addons.runbot.models.docker.USERGID', '1337'),
            ):
            docker_render = self.env.ref('runbot.docker_default').dockerfile.replace('\n\n', '\n')
            docker_render = '\n'.join(line for line in docker_render.split('\n') if line and line[0] != '#')
            docker_render = re.sub(r'google-chrome-stable_\d{3}\.\d\.\d{1,4}\.\d{1,4}-\d', 'google-chrome-stable_xxx.x.xxxx.xx-x', docker_render)

        self.assertEqual(
r"""FROM ubuntu:noble
ENV LANG C.UTF-8
USER root
RUN set -x ; \
    apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends apt-transport-https build-essential ca-certificates curl faketime file fonts-freefont-ttf fonts-noto-cjk gawk gnupg gsfonts libldap2-dev libjpeg9-dev libsasl2-dev libxslt1-dev lsb-release npm ocrmypdf sed sudo unzip xfonts-75dpi zip zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
RUN set -x ; \
    apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends publicsuffix python3 flake8 python3-dbfread python3-dev python3-gevent python3-pip python3-setuptools python3-wheel python3-markdown python3-mock python3-phonenumbers python3-websocket python3-google-auth libpq-dev pylint python3-jwt python3-asn1crypto python3-html2text python3-suds python3-xmlsec python3-markdown2 python3-aiosmtpd python3-paramiko \
    && rm -rf /var/lib/apt/lists/*
RUN curl -sSL https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-2/wkhtmltox_0.12.6.1-2.jammy_amd64.deb -o /tmp/wkhtml.deb \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get -y install --no-install-recommends --fix-missing -qq /tmp/wkhtml.deb \
    && rm -rf /var/lib/apt/lists/* \
    && rm /tmp/wkhtml.deb
ENV NODE_PATH=/usr/lib/node_modules/
ENV npm_config_prefix=/usr
RUN npm install --force -g rtlcss@3.4.0 es-check@6.0.0 eslint@8.1.0 prettier@2.7.1 eslint-config-prettier@8.5.0 eslint-plugin-prettier@4.2.1
ADD https://raw.githubusercontent.com/odoo/odoo/master/debian/control /tmp/control.txt
RUN curl -sSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /etc/apt/trusted.gpg.d/psql_client.asc \
    && echo "deb http://apt.postgresql.org/pub/repos/apt/ `lsb_release -s -c`-pgdg main" > /etc/apt/sources.list.d/pgclient.list \
    && apt-get update \
    && sed -n '/^Depends:/,/^[A-Z]/p' /tmp/control.txt \
        | awk '/^ [a-z]/ { gsub(/,/,"") ; gsub(" ", "") ; print $NF }' | sort -u \
        | DEBIAN_FRONTEND=noninteractive xargs apt-get install -y -qq --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
RUN curl -sSL https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_xxx.x.xxxx.xx-x_amd64.deb -o /tmp/chrome.deb \
    && apt-get update \
    && apt-get -y install --no-install-recommends /tmp/chrome.deb \
    && rm /tmp/chrome.deb
RUN deluser ubuntu
RUN groupadd -g 1337 TestUser && useradd --create-home -u 4242 -g TestUser -G audio,video TestUser
USER TestUser
ENV PIP_BREAK_SYSTEM_PACKAGES=1
RUN python3 -m pip install --no-cache-dir ebaysdk==2.1.5 pdf417gen==0.7.1
ADD --chown=TestUser https://raw.githubusercontent.com/odoo/odoo/master/requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /tmp/requirements.txt
USER TestUser""", docker_render)

    def test_dockerfile_base_fields(self):
        dockerfile = self.env['runbot.dockerfile'].create({
            'name': 'TestsUbuntuFocal_Chrome86',
            'to_build': True,
            'layer_ids': [
                Command.create({
                    'name': 'Customized base',
                    'reference_dockerfile_id': self.env.ref('runbot.docker_default').id,
                    'values': {
                        'chrome_version': '86.0.4240.183-1',
                    },
                    'layer_type': 'reference_file',
                }),
                Command.create({
                    'name': 'Customized base',
                    'packages': 'babel==2.8.0',
                    'layer_type': 'reference_layer',
                    'reference_docker_layer_id': self.env.ref('runbot.docker_layer_pip_packages_template').id,
                }),
            ],
        })

        self.assertEqual(dockerfile.image_tag, 'odoo:TestsUbuntuFocal_Chrome86')
        self.assertIn('86.0.4240.183-1', dockerfile.dockerfile)
        self.assertIn('pip install --no-cache-dir babel==2.8.0', dockerfile.dockerfile)

        # test layer update
        dockerfile.layer_ids[0].values = {**dockerfile.layer_ids[0].values, 'chrome_version': '87.0.4240.183-1'}

        self.assertIn('Install chrome with values {"chrome_version": "87.0.4240.183-1"}', dockerfile.dockerfile)

    def test_dockerfile_variant(self):
        default_dockerfile = self.env.ref('runbot.docker_default')
        dockerfile = self.env['runbot.dockerfile'].create({
            'name': 'Documentation',
            'parent_id': default_dockerfile.id,
            'layer_ids': [
                Command.create({
                    'name': 'Custom layer',
                    'layer_type': 'raw',
                    'content': 'some_random_command',
                })
            ]
        })
        expected_tag = default_dockerfile.image_tag + '.documentation'
        self.assertEqual(dockerfile.image_tag, expected_tag)
        self.assertEqual(dockerfile.image_future_tag, expected_tag + '.future')
        self.assertIn('some_random_command', dockerfile.dockerfile)
        self.assertIn('RUN python3 -m pip install --no-cache-dir', dockerfile.dockerfile)

    def test_dockerfile_cycle_parent(self):
        default_dockerfile = self.env.ref('runbot.docker_default')
        dockerfile = self.env['runbot.dockerfile'].create({
            'name': 'Documentation',
            'parent_id': default_dockerfile.id,
        })
        with self.assertRaises(exceptions.ValidationError):
            default_dockerfile.parent_id = dockerfile

    def test_dockerfile_variant_unique(self):
        default_dockerfile = self.env.ref('runbot.docker_default')
        self.env['runbot.dockerfile'].create({
            'name': 'Documentation',
            'parent_id': default_dockerfile.id,
        })
        with mute_logger('odoo.sql_db'), self.assertRaises(UniqueViolation):
            self.env['runbot.dockerfile'].create({
                'name': 'Documentation',
                'parent_id': default_dockerfile.id,
            })
        # But it works with another name
        self.env['runbot.dockerfile'].create({
            'name': 'Documentation2',
            'parent_id': default_dockerfile.id,
        })


@tagged('-at_install', 'post_install')
class TestDockerfileCache(RunbotCase, HttpCase):
    def test_dockerfile_get_cached_content(self):
        dockerfile = self.env['runbot.dockerfile'].create({
            'name': 'TestsAddCache',
            'to_build': True,
            'layer_ids': [
                Command.create({
                    'name': 'CacheAddTest',
                    'layer_type': 'raw',
                    'content': 'some useless content',
                }),
            ],
        })

        self.start_patcher('docker_username', 'odoo.addons.runbot.models.docker.USERNAME', new='TestUser')

        expected_content = """# CacheAddTest
some useless content

USER TestUser
"""

        self.start_patcher('hardlink_to', 'odoo.addons.runbot.models.docker.Path.hardlink_to')
        self.start_patcher('path_unlink', 'odoo.addons.runbot.models.docker.Path.unlink')
        content = dockerfile._get_cached_content('/tmp/fake_build_path')
        self.assertEqual(content, expected_content, 'Dockerfile without "ADD" should be left unchanged')

        raw_layer = """FROM ubuntu:noble
ADD https://nowhere.example.org/nothing.txt /data/nothing.txt
"""

        expected_content = """# CacheAddTest
FROM ubuntu:noble
ADD https://nowhere.example.org/nothing.txt /data/nothing.txt


USER TestUser
"""
        dockerfile.layer_ids[0].content = raw_layer
        content = dockerfile._get_cached_content('/tmp/fake_build_path')
        self.assertEqual(content, expected_content, 'Dockerfile without "#CACHE" directive should be left unchanged')

        # Here we start the useful cache tests
        raw_layer = """FROM ubuntu:noble
# CACHE 60
ADD https://nowhere.example.org/nothing.txt /data/nothing.txt
"""

        expected_content = """# CacheAddTest
FROM ubuntu:noble
# CACHE 60
COPY _data_nothing_txt /data/nothing.txt


USER TestUser
"""
        mock_response = MagicMock()
        mock_response.iter_content.return_value = [b'small file content']
        self.start_patcher('docker_requests_get', 'odoo.addons.runbot.models.docker.requests.get', return_value=mock_response)

        # 1 - The cache file does not exists yet
        self.start_patcher('docker_path_exists', 'odoo.addons.runbot.models.docker.Path.exists', return_value=False)
        dockerfile.layer_ids[0].content = raw_layer
        with patch('odoo.addons.runbot.models.docker.Path.open', mock_open()) as cache_file_mock:
            content = dockerfile._get_cached_content('/tmp/fake_build_path')
            cache_file_mock.assert_called_once_with('wb')
        self.assertEqual(content, expected_content, 'Dockerfile with "#CACHE" should change the ADD directive to COPY')

        # 2 - The cache file exists but the cache duration is expired
        self.patchers['docker_path_exists'].return_value = True
        self.start_patcher('docker_path_lstat', 'odoo.addons.runbot.models.docker.Path.lstat')
        self.patchers['docker_path_lstat'].return_value.st_mtime = time.time() - 100
        with patch('odoo.addons.runbot.models.docker.Path.open', mock_open()) as cache_file_mock:
            content = dockerfile._get_cached_content('/tmp/fake_build_path')
            cache_file_mock.assert_called_once_with('wb')
        self.assertEqual(content, expected_content, 'Dockerfile with "#CACHE" should change the ADD directive to COPY')

        # 3 - The cache file exists but the cache duration is not expired
        self.start_patcher('docker_path_touch', 'odoo.addons.runbot.models.docker.Path.touch', return_value=True)
        self.patchers['docker_path_lstat'].return_value.st_mtime = time.time() - 2
        with patch('odoo.addons.runbot.models.docker.Path.open', mock_open()) as cache_file_mock:
            content = dockerfile._get_cached_content('/tmp/fake_build_path')
            cache_file_mock.assert_not_called()
        self.assertEqual(content, expected_content, 'Dockerfile with "#CACHE" should change the ADD directive to COPY')
        self.patchers['docker_path_touch'].assert_not_called()

        # 4 - The cache file does not exists yet but the there is an error while downloading
        self.patchers['docker_path_exists'].return_value = False
        self.patchers['docker_requests_get'].side_effect = HTTPError

        dockerfile.layer_ids[0].content = raw_layer
        with patch('odoo.addons.runbot.models.docker.Path.open', mock_open()) as cache_file_mock:
            with self.assertRaises(HTTPError, msg='HTTPError Exception should be reraised during cache download'):
                content = dockerfile._get_cached_content('/tmp/fake_build_path')

    def test_dockerfile_build_with_cached_content(self):
        dockerfile = self.env['runbot.dockerfile'].create({
            'name': 'TestsAddCache',
            'to_build': True,
            'layer_ids': [
                Command.create({
                    'name': 'CacheAddTest',
                    'layer_type': 'raw',
                    'content': 'some useless content',
                }),
            ],
        })

        dockerfile.layer_ids[0].content = """# Cache Test
FROM ubuntu:noble
# CACHE 60
ADD https://nowhere.example.org/nothing.txt /data/nothing.txt
"""

        expected_content = """# Cache Test
FROM ubuntu:noble
# CACHE 60
COPY _data_nothing_txt /data/nothing.txt


USER TestUser
"""

        self.start_patcher('docker_username', 'odoo.addons.runbot.models.docker.USERNAME', new='TestUser')
        self.start_patcher('docker_path_exists', 'odoo.addons.runbot.models.docker.Path.exists', return_value=False)
        self.start_patcher('docker_path_hardlink_to', 'odoo.addons.runbot.models.docker.Path.hardlink_to')
        self.start_patcher('docker_get_docker_metadata', 'odoo.addons.runbot.models.docker.Dockerfile._get_docker_metadata')

        mock_response = MagicMock()
        mock_response.iter_content.return_value = [b'small file content']
        self.start_patcher('docker_requests_get', 'odoo.addons.runbot.models.docker.requests.get', return_value=mock_response)

        self.patchers['docker_build'].return_value = {
            'image_id': 'xxx',
            'success': True,
            'duration': 69,
            'image': 'd0d0caca',
            'msg': '',
        }

        with patch('odoo.addons.runbot.models.docker.Path.open', mock_open()) as cache_file_mock:
            with patch('builtins.open', mock_open()) as dockerfile_file:
                dockerfile._build()
        cache_file_mock.assert_called_once_with('wb')
        dockerfile_file_handle = dockerfile_file()
        dockerfile_file_handle.write.assert_called_once_with(expected_content)
        self.patchers['docker_path_hardlink_to'].assert_called()
        self.patchers['docker_get_docker_metadata'].assert_called()
