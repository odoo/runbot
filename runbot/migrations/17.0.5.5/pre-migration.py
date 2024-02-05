# -*- coding: utf-8 -*-

import logging

try:
    from odoo.upgrade import util
except ImportError:
    util = None

_logger = logging.getLogger(__name__)

def migrate(cr, version):
    if util:
        util.remove_field(cr, "runbot.build.config", "message_main_attachment_id")
        util.remove_field(cr, "runbot.build.config.step", "message_main_attachment_id")
        util.remove_field(cr, "runbot.build.error", "message_main_attachment_id")
        util.remove_field(cr, "runbot.error.regex", "message_main_attachment_id")
        util.remove_field(cr, "runbot.bundle", "message_main_attachment_id")
        util.remove_field(cr, "runbot.codeowner", "message_main_attachment_id")
        util.remove_field(cr, "runbot.dockerfile", "message_main_attachment_id")
        util.remove_field(cr, "runbot.host", "message_main_attachment_id")
        util.remove_field(cr, "runbot.trigger", "message_main_attachment_id")
        util.remove_field(cr, "runbot.remote", "message_main_attachment_id")
        util.remove_field(cr, "runbot.repo", "message_main_attachment_id")
        util.remove_field(cr, "runbot.team", "message_main_attachment_id")
    else:
        _logger.error('Missing utils, cannot migrate to 17.0')