# -*- coding: utf-8 -*-
import logging

from ..common import os
import re

from odoo import models, fields, api
from odoo.exceptions import ValidationError

VALUE_PATTERN = r"\(\?P\<value\>.+\)"  # used to verify value group pattern

_logger = logging.getLogger(__name__)


class BuildStatRegex(models.Model):
    """ A regular expression to extract a float/int value from  a log file
        The regulare should contain a named group like '(?P<value>.+)'.
        The result will be a key/value like {name: value}
        A second named group '(?P<key>.+)' can bu used to augment the key name
        like {name.key_result: value}
        A 'generic' regex will be used when no regex are defined on a make_stat
        step.
    """

    _name = "runbot.build.stat.regex"
    _description = "Statistics regex"
    _order = 'sequence,id'

    name = fields.Char("Key Name")
    regex = fields.Char("Regular Expression")
    description = fields.Char("Description")
    generic = fields.Boolean('Generic', help='Executed when no regex on the step', default=True)
    config_step_ids = fields.Many2many('runbot.build.config.step', string='Config Steps')
    sequence = fields.Integer('Sequence')

    @api.constrains("name", "regex")
    def _check_regex(self):
        for rec in self:
            try:
                r = re.compile(rec.regex)
            except re.error as e:
                raise ValidationError("Unable to compile regular expression: %s" % e)
            # verify that a named group exist in the pattern
            if not re.search(VALUE_PATTERN, r.pattern):
                raise ValidationError(
                    "The regular expresion should contain the name group pattern 'value' e.g: '(?P<value>.+)'"
                )

    def _find_in_file(self, file_path):
        """ Search file regexes and write stats
            returns a dict of key:values
        """
        if not os.path.exists(file_path):
            return {}
        stats_matches = {}
        with open(file_path, "r") as log_file:
            data = log_file.read()
            for build_stat_regex in self:
                current_stat_matches = {}
                for match in re.finditer(build_stat_regex.regex, data):
                    group_dict = match.groupdict()
                    try:
                        value = float(group_dict.get("value"))
                    except ValueError:
                        _logger.warning(
                            'The matched value (%s) of "%s" cannot be converted into float',
                            group_dict.get("value"), build_stat_regex.regex
                        )
                        continue
                    current_stat_matches[group_dict.get('key', 'value')] = value
                stats_matches[build_stat_regex.name] = current_stat_matches
        return stats_matches
