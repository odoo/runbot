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
from datetime import timedelta

from babel.dates import format_timedelta

from odoo.tools.misc import DEFAULT_SERVER_DATETIME_FORMAT

_logger = logging.getLogger(__name__)

dest_reg = re.compile(r'^\d{5,}-.{1,32}-[\da-f]{6}(.*)*$')

class Commit():
    def __init__(self, repo, sha):
        self.repo = repo
        self.sha = sha

    def _source_path(self, *path):
        return self.repo._source_path(self.sha, *path)

    def export(self):
        return self.repo._git_export(self.sha)

    def read_source(self, file, mode='r'):
        file_path = self._source_path(file)
        try:
            with open(file_path, mode) as f:
                return f.read()
        except:
            return False

    def __str__(self):
        return '%s:%s' % (self.repo.short_name, self.sha)


def fqdn():
    return socket.getfqdn()


def time2str(t):
    return time.strftime(DEFAULT_SERVER_DATETIME_FORMAT, t)


def dt2time(datetime):
    """Convert datetime to time"""
    return time.mktime(time.strptime(datetime, DEFAULT_SERVER_DATETIME_FORMAT))


def now():
    return time.strftime(DEFAULT_SERVER_DATETIME_FORMAT)


def grep(filename, string):
    if os.path.isfile(filename):
        return find(filename, string) != -1
    return False


def find(filename, string):
    return open(filename).read().find(string)


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
    return format_timedelta(
        timedelta(seconds=time),
        format="narrow",
        threshold=2.1,
    )

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
