# -*- coding: utf-8 -*-

import contextlib
import itertools
import logging
import psycopg2
import re
import requests
import socket
import time
import os

from collections import OrderedDict
from datetime import timedelta
from babel.dates import format_timedelta
from markupsafe import Markup

from odoo.tools.misc import DEFAULT_SERVER_DATETIME_FORMAT, html_escape

_logger = logging.getLogger(__name__)

dest_reg = re.compile(r'^\d{5,}-.+$')


class RunbotException(Exception):
    pass


def fqdn():
    return socket.gethostname()


def time2str(t):
    return time.strftime(DEFAULT_SERVER_DATETIME_FORMAT, t)


def dt2time(datetime):
    """Convert datetime to time"""
    return time.mktime(datetime.timetuple())


def now():
    return time.strftime(DEFAULT_SERVER_DATETIME_FORMAT)


def findall(filename, pattern):
    return set(re.findall(pattern, open(filename).read()))


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


def time_delta(time):
    if isinstance(time, timedelta):
        return time
    return timedelta(seconds=-time)


def s2human(time):
    """Convert a time in second into an human readable string"""
    return format_timedelta(
        time_delta(time),
        format="narrow",
        threshold=2.1,
    )


def s2human_long(time):
    return format_timedelta(
        time_delta(time),
        threshold=2.1,
        add_direction=True, locale='en'
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

@contextlib.contextmanager
def local_pg_cursor(db_name):
    cnx = None
    try:
        cnx = psycopg2.connect(f"dbname={db_name}")
        yield cnx.cursor()
    finally:
        if cnx:
            cnx.commit()
            cnx.close()

def list_local_dbs(additionnal_conditions=None):
    additionnal_condition_str = ''
    if additionnal_conditions:
        additionnal_condition_str = 'AND (%s)' % ' OR '.join(additionnal_conditions)
    with local_pgadmin_cursor() as local_cr:
        local_cr.execute("""
            SELECT datname
                FROM pg_database
                WHERE pg_get_userbyid(datdba) = current_user
                %s
        """ % additionnal_condition_str)
        return [d[0] for d in local_cr.fetchall()]


def pseudo_markdown(text):
    text = html_escape(text)

    # first, extract code blocs:
    codes = []
    def code_remove(match):
        codes.append(match.group(1))
        return f'<code>{len(codes)-1}</code>'

    patterns = {
        r'`(.+?)`': code_remove,
        r'\*\*(.+?)\*\*': '<strong>\\g<1></strong>',
        r'~~(.+?)~~': '<del>\\g<1></del>',  # it's not official markdown but who cares
        r'__(.+?)__': '<ins>\\g<1></ins>',  # same here, maybe we should change the method name
        r'\r?\n': '<br/>',
    }

    for p, b in patterns.items():
        text = re.sub(p, b, text, flags=re.DOTALL)

    # icons
    re_icon = re.compile(r'@icon-([a-z0-9-]+)')
    text = re_icon.sub('<i class="fa fa-\\g<1>"></i>', text)

    # links
    re_links = re.compile(r'\[(.+?)\]\((.+?)\)')
    text = re_links.sub('<a href="\\g<2>">\\g<1></a>', text)

    def code_replace(match):
        return f'<code>{codes[int(match.group(1))]}</code>'

    text = Markup(re.sub(r'<code>(\d+)</code>', code_replace, text, flags=re.DOTALL))
    return text


def _make_github_session(token):
    session = requests.Session()
    if token:
        session.auth = (token, 'x-oauth-basic')
    session.headers.update({'Accept': 'application/vnd.github.she-hulk-preview+json'})
    return session
