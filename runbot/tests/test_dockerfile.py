# -*- coding: utf-8 -*-
import getpass
import logging
import os
import re

from odoo import Command
from unittest.mock import patch, mock_open

from odoo.tests.common import tagged, HttpCase
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
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends apt-transport-https build-essential ca-certificates curl file fonts-freefont-ttf fonts-noto-cjk gawk gnupg gsfonts libldap2-dev libjpeg9-dev libsasl2-dev libxslt1-dev lsb-release npm ocrmypdf sed sudo unzip xfonts-75dpi zip zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
RUN set -x ; \
    apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends publicsuffix python3 flake8 python3-dbfread python3-dev python3-gevent python3-pip python3-setuptools python3-wheel python3-markdown python3-mock python3-phonenumbers python3-websocket python3-google-auth libpq-dev pylint python3-jwt python3-asn1crypto python3-html2text python3-suds python3-xmlsec python3-markdown2 \
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
            'name': 'Tests Ubuntu Focal (20.0)[Chrome 86]',
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

        self.assertEqual(dockerfile.image_tag, 'odoo:TestsUbuntuFocal20.0Chrome86')
        self.assertIn('86.0.4240.183-1', dockerfile.dockerfile)
        self.assertIn('pip install --no-cache-dir babel==2.8.0', dockerfile.dockerfile)

        # test layer update
        dockerfile.layer_ids[0].values = {**dockerfile.layer_ids[0].values, 'chrome_version': '87.0.4240.183-1'}

        self.assertIn('Install chrome with values {"chrome_version": "87.0.4240.183-1"}', dockerfile.dockerfile)
