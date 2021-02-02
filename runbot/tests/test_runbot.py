# -*- coding: utf-8 -*-
import logging

from .common import RunbotCase

_logger = logging.getLogger(__name__)


class TestRunbot(RunbotCase):

    def test_warning_from_runbot_abstract(self):
        warning = self.env['runbot.runbot'].warning('Test warning message')

        self.assertTrue(self.env['runbot.warning'].browse(warning.id).exists())
