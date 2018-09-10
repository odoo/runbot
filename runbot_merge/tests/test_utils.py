# -*- coding: utf-8 -*-
import re


class re_matches:
    def __init__(self, pattern, flags=0):
        self._r = re.compile(pattern, flags)

    def __eq__(self, text):
        return self._r.match(text)

    def __repr__(self):
        return '~' + self._r.pattern + '~'
