# -*- coding: utf-8 -*-

import contextlib
import fcntl
import itertools
import logging
import os
import psycopg2
import re
import socket
import time

from collections import OrderedDict

from odoo.tools.misc import DEFAULT_SERVER_DATETIME_FORMAT

_logger = logging.getLogger(__name__)


def fqdn():
    return socket.getfqdn()


def time2str(t):
    time.strftime(DEFAULT_SERVER_DATETIME_FORMAT, t)


def dt2time(datetime):
    """Convert datetime to time"""
    return time.mktime(time.strptime(datetime, DEFAULT_SERVER_DATETIME_FORMAT))


def now():
    return time.strftime(DEFAULT_SERVER_DATETIME_FORMAT)


def lock(filename):
    fd = os.open(filename, os.O_CREAT | os.O_RDWR, 0o600)
    if hasattr(os, 'set_inheritable'):
        os.set_inheritable(fd, True)  # needed since pep-446
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def locked(filename):
    result = False
    try:
        fd = os.open(filename, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        os.close(fd)
        return False
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:  # since pep-3151 fcntl raises OSError and IOError is now an alias of OSError
        result = True
    finally:
        os.close(fd)
    return result


def grep(filename, string):
    if os.path.isfile(filename):
        return open(filename).read().find(string) != -1
    return False


def uniq_list(l):
    return OrderedDict.fromkeys(l).keys()


def flatten(list_of_lists):
    return list(itertools.chain.from_iterable(list_of_lists))


def rfind(filename, pattern):
    """Determine in something in filename matches the pattern"""
    if os.path.isfile(filename):
        regexp = re.compile(pattern, re.M)
        with open(filename, 'r') as f:
            if regexp.findall(f.read()):
                return True
    return False


def s2human(time):
    """Convert a time in second into an human readable string"""
    for delay, desc in [(86400, 'd'),(3600, 'h'),(60, 'm')]:
        if time >= delay:
            return str(int(time / delay)) + desc
    return str(int(time)) + "s"


@contextlib.contextmanager
def local_pgadmin_cursor():
    cnx = None
    try:
        cnx = psycopg2.connect("dbname=postgres")
        cnx.autocommit = True  # required for admin commands
        yield cnx.cursor()
    finally:
        if cnx:
            cnx.close()
