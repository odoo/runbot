# -*- coding: utf-8 -*-

import contextlib
import functools
import itertools
import logging
import os
import psycopg2
import re
import requests
import socket
import time

from babel.dates import LC_TIME, TIMEDELTA_UNITS, Locale
from collections import OrderedDict
from datetime import timedelta
from markupsafe import Markup

from odoo.osv import expression
from odoo.tools.misc import DEFAULT_SERVER_DATETIME_FORMAT, file_open, html_escape, OrderedSet

_logger = logging.getLogger(__name__)

dest_reg = re.compile(r'^\d{5,}-.+$')


def transactioncache(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        assert not self.ids
        cache = self.env.cr.cache
        method_key = method
        params_key = (args, frozenset(kwargs.items()))
        # should check id key is serializable
        if method_key not in cache:
            cache[method_key] = {}
        if params_key not in cache[method_key]:
            cache[method_key][params_key] = method(self.with_context({}), *args, **kwargs)
        return cache[method_key][params_key]
    wrapper.clear_transaction_cache = lambda self: self.env.cr.cache.pop(method, None)
    return wrapper

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


def tail(filename, n=10):
    if os.path.isfile(filename):
        return file_open(filename).readlines()[-n:]
    return ''


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
            result = regexp.findall(f.read())
            return result or False
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


def precise_s2human(time):
    hours, remainder = divmod(time, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if not hours and (seconds > 0 or not parts):
        parts.append(f"{seconds}s")
    return ''.join(parts)


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
        return f'<code>{len(codes) - 1}</code>'

    escape = r'(?<!\\)(?:(?:\\\\)*)'

    text = re.sub(rf'{escape}`(.+?{escape})`', code_remove, text, flags=re.DOTALL)

    patterns = {
        r'\*\*(.+?)\*\*': '<strong>\\g<1></strong>',
        r'~~(.+?)~~': '<del>\\g<1></del>',  # it's not official markdown but who cares
        r'__(.+?)__': '<ins>\\g<1></ins>',  # same here, maybe we should change the method name
        r'\r?\n': '<br/>\n',
    }

    for p, b in patterns.items():
        text = re.sub(p, b, text, flags=re.DOTALL)

    # icons
    re_icon = re.compile(r'@icon-([a-z0-9-]+)')
    text = re_icon.sub('<i class="fa fa-\\g<1>"></i>', text)

    # links
    re_links = re.compile(rf'{escape}\[(.+?){escape}\]{escape}\(((http|/).+?{escape})\)')
    text = re_links.sub('<a href="\\g<2>">\\g<1></a>', text)

    def code_replace(match):
        return f'<code>{codes[int(match.group(1))]}</code>'

    text = Markup(re.sub(r'<code>(\d+)</code>', code_replace, text, flags=re.DOTALL))
    text = markdown_unescape(text)
    return text

patterns = ['\\', '[', ']', '(', ')', '_', '*', '#', '`']

def markdown_escape(text):
    text = str(text)
    for pat in patterns:
        text = text.replace(pat, rf'\{pat}')
    return text


def markdown_unescape(text):
    for pat in patterns:
        text = text.replace(rf'\{pat}', pat)
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


# Based on Odoo TagsSelector from master on 2025-06-13
class TestTagsParser:
    filter_spec_re = re.compile(r'''
                                ^
                                ([+-]?)                     # operator_re
                                (\*|\w*)                    # tag_re
                                (\/[\w\/\.-]+.py)?           # file_re
                                (?:\/(\w+))?                # module_re
                                (?::(\w*))?                 # test_class_re
                                (?:\.(\w*))?                # test_method_re
                                (?:\[(.*)\])?               # parameters
                                $''', re.VERBOSE)  # [-][tag][/module][:class][.method][[params]]

    def __init__(self, test_tags):
        parts = re.split(r',(?![^\[]*\])', test_tags)  # split on all comma not inside [] (not followed by ])
        filter_specs = [t.strip() for t in parts if t.strip()]
        self.exclude = set()
        self.include = set()
        self.parameters = OrderedSet()

        for filter_spec in filter_specs:
            match = self.filter_spec_re.match(filter_spec)
            if not match:
                _logger.error('Invalid tag %s', filter_spec)
                continue

            sign, tag, file_path, module, klass, method, parameters = match.groups()
            is_include = sign != '-'
            is_exclude = not is_include

            if not tag and is_include:
                # including /module:class.method implicitly requires 'standard'
                tag = 'standard'
            elif not tag or tag == '*':
                # '*' indicates all tests (instead of 'standard' tests only)
                tag = None
            test_filter = (tag, module, klass, method, file_path)

            if parameters:
                # we could check here that test supports negated parameters
                self.parameters.add((test_filter, ('-' if is_exclude else '+', parameters)))
                is_exclude = False

            if is_include:
                self.include.add(test_filter)
            if is_exclude:
                self.exclude.add(test_filter)

        if (self.exclude or self.parameters) and not self.include:
            self.include.add(('standard', None, None, None, None))

    def test_tags_to_search_domain(self, exclude_error_id=None):
        search_domains = []
        for include in self.include:
            _, test_module, test_class, test_method, file_path = include
            module_path = file_path or ((test_module or '') + '%')
            test_class = test_class or '%'
            test_method = test_method or '%'
            search_pattern = f'{module_path}:{test_class}.{test_method}'
            tag_domain = [('canonical_tags', 'like', f'{search_pattern}')]
            if exclude_error_id:
                tag_domain.append(('id', '!=', exclude_error_id))
            search_domains.append(tag_domain)
        search_domain = expression.OR(search_domains)
        return search_domain
