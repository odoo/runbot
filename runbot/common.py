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
from babel.dates import LC_TIME, Locale, TIMEDELTA_UNITS
from markupsafe import Markup

from odoo.tools.misc import DEFAULT_SERVER_DATETIME_FORMAT, html_escape, file_open

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
    return set(re.findall(pattern, file_open(filename).read()))


def grep(filename, string):
    if os.path.isfile(filename):
        return find(filename, string) != -1
    return False


def find(filename, string):
    return file_open(filename).read().find(string)


def uniq_list(l):
    return OrderedDict.fromkeys(l).keys()


def flatten(list_of_lists):
    return list(itertools.chain.from_iterable(list_of_lists))


def rfind(filename, pattern):
    """Determine in something in filename matches the pattern"""
    if os.path.isfile(filename):
        regexp = re.compile(pattern, re.M)
        with file_open(filename, 'r') as f:
            if regexp.findall(f.read()):
                return True
    return False


def time_delta(time):
    if isinstance(time, timedelta):
        return time
    return timedelta(seconds=-time)

from babel.dates import format_timedelta as _format_timedelta


def format_timedelta(delta, granularity='second', max_unit=None, threshold=.85,
                     add_direction=False, format='long',
                     locale=LC_TIME):
    """
    Modified version of Dates.format_timedelta
    """
    if format not in ('narrow', 'short', 'long'):
        raise TypeError('Format must be one of "narrow", "short" or "long"')
    if isinstance(delta, timedelta):
        seconds = int((delta.days * 86400) + delta.seconds)
    else:
        seconds = delta
    locale = Locale.parse(locale)

    def _iter_patterns(a_unit):
        if add_direction:
            unit_rel_patterns = locale._data['date_fields'][a_unit]
            if seconds >= 0:
                yield unit_rel_patterns['future']
            else:
                yield unit_rel_patterns['past']
        a_unit = 'duration-' + a_unit
        yield locale._data['unit_patterns'].get(a_unit, {}).get(format)

    for unit, secs_per_unit in TIMEDELTA_UNITS:
        if max_unit and unit != max_unit:
            continue
        max_unit = None
        value = abs(seconds) / secs_per_unit
        if value >= threshold or unit == granularity:
            if unit == granularity and value > 0:
                value = max(1, value)
            value = int(round(value))
            plural_form = locale.plural_form(value)
            pattern = None
            for patterns in _iter_patterns(unit):
                if patterns is not None:
                    pattern = patterns[plural_form]
                    break
            # This really should not happen
            if pattern is None:
                return u''
            return pattern.replace('{0}', str(value))

    return u''


def s2human(time):
    """Convert a time in second into an human readable string"""
    return format_timedelta(
        time_delta(time),
        max_unit='hour',
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


def make_github_session(token):
    session = requests.Session()
    if token:
        session.auth = (token, 'x-oauth-basic')
    session.headers.update({'Accept': 'application/vnd.github.she-hulk-preview+json'})
    return session

def sanitize(name):
    for i in ['@', ':', '/', '\\', '..']:
        name = name.replace(i, '_')
    return name


class ReProxy():
    @classmethod
    def match(cls, *args, **kwrags):
        return re.match(*args, **kwrags)

    @classmethod
    def search(cls, *args, **kwrags):
        return re.search(*args, **kwrags)

    @classmethod
    def compile(cls, *args, **kwrags):
        return re.compile(*args, **kwrags)

    @classmethod
    def findall(cls, *args, **kwrags):
        return re.findall(*args, **kwrags)

    VERBOSE = re.VERBOSE
    MULTILINE = re.MULTILINE

