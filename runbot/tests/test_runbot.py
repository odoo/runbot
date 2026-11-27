# -*- coding: utf-8 -*-
import logging
import os
import signal
from unittest.mock import patch

from odoo.tools import file_open_temporary_directory

from .common import RunbotCase

_logger = logging.getLogger(__name__)


class TestRunbot(RunbotCase):

    def test_warning_from_runbot_abstract(self):
        warning = self.env['runbot.runbot']._warning('Test warning message')

        self.assertTrue(self.env['runbot.warning'].browse(warning.id).exists())


class TestNginx(RunbotCase):

    def test_reload_nginx(self):
        self.stop_patcher('reload_nginx')
        self.stop_patcher('isfile')
        tmp_dir = self.enterContext(file_open_temporary_directory(self.env))
        self.patch(type(self.env['runbot.runbot']), '_root', lambda self: tmp_dir)
        call = self.enterContext(patch('odoo.addons.runbot.models.runbot.subprocess.call', return_value=0))
        kill = self.enterContext(patch('odoo.addons.runbot.models.runbot.os.kill'))
        nginx_dir = os.path.join(tmp_dir, 'nginx')
        start_cmd = ['/usr/sbin/nginx', '-p', nginx_dir, '-c', 'nginx.conf']
        warning_message = f"Host {self.env['runbot.host']._get_current_name()}: nginx was not running"

        def reload_nginx():
            call.reset_mock()
            kill.reset_mock()
            self.env['runbot.runbot']._reload_nginx()

        # new config, nginx not running -> start
        reload_nginx()
        call.assert_called_once_with(start_cmd)
        self.assertFalse(self.env['runbot.warning'].search([('message', '=', warning_message)]))

        # config unchanged, nginx not running (reboot, crash) -> start and warn
        reload_nginx()
        call.assert_called_once_with(start_cmd)
        self.assertEqual(self.env['runbot.warning'].search([('message', '=', warning_message)]).count, 1)

        # config unchanged, nginx running -> nothing to do
        with open(os.path.join(nginx_dir, 'nginx.pid'), 'w') as f:
            f.write('4242\n')
        reload_nginx()
        kill.assert_called_once_with(4242, 0)
        call.assert_not_called()

        # config changed, nginx running -> reload
        with open(os.path.join(nginx_dir, 'nginx.conf'), 'w') as f:
            f.write('outdated')
        reload_nginx()
        kill.assert_called_once_with(4242, signal.SIGHUP)
        call.assert_not_called()
