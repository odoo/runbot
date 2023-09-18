# -*- coding: utf-8 -*-

from . import controllers
from . import models
from . import common
from . import container
from . import wizards

import logging
import threading
from odoo.http import request

class UserFilter(logging.Filter):
    def filter(self, record):  # noqa: A003
        message_parts = record.msg.split(' ', 2)
        if message_parts[1] == '-':
            uid = getattr(threading.current_thread(), 'uid', None)
            if uid is None:
                return True
            user_name = 'user'
            if hasattr(threading.current_thread(), 'user_name'):
                user_name = threading.current_thread().user_name
                del(threading.current_thread().user_name)
            message_parts[1] = f'({user_name}:{uid})'
            record.msg = ' '.join(message_parts)
        return True


def runbot_post_load():
    logging.getLogger('werkzeug').addFilter(UserFilter())
