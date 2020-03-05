# -*- coding: utf-8 -*-
import re

from odoo import fields, models, api
from odoo.exceptions import ValidationError
from odoo.addons.runbot.models.build_stat_regex import VALUE_PATTERN


class StatRegexWizard(models.TransientModel):
    _name = 'runbot.build.stat.regex.wizard'
    _description = "Stat Regex Wizard"

    name = fields.Char("Key Name")
    regex = fields.Char("Regular Expression")
    description = fields.Char("Description")
    generic = fields.Boolean('Generic', help='Executed when no regex on the step', default=True)
    test_text = fields.Text("Test text")
    key = fields.Char("Key")
    value = fields.Float("Value")
    message = fields.Char("Wizard message")

    def _validate_regex(self):
        try:
            regex = re.compile(self.regex)
        except re.error as e:
            raise ValidationError("Unable to compile regular expression: %s" % e)
        if not re.search(VALUE_PATTERN, regex.pattern):
            raise ValidationError(
                "The regular expresion should contain the name group pattern 'value' e.g: '(?P<value>.+)'"
            )

    @api.onchange('regex', 'test_text')
    def _onchange_regex(self):
        key = ''
        value = False
        self.message = ''
        if self.regex and self.test_text:
            self._validate_regex()
            match = re.search(self.regex, self.test_text)
            if match:
                group_dict = match.groupdict()
                try:
                    value = float(group_dict.get("value"))
                except ValueError:
                    raise ValidationError('The matched value (%s) of "%s" cannot be converted into float' % (group_dict.get("value"), self.regex))
                key = (
                    "%s.%s" % (self.name, group_dict["key"])
                    if "key" in group_dict
                    else self.name
                )
            else:
                self.message = 'No match !'
            self.key = key
            self.value = value

    def save(self):
        if self.regex and self.test_text:
            self._validate_regex()
            stat_regex = self.env['runbot.build.stat.regex'].create({
                'name': self.name,
                'regex': self.regex,
                'description': self.description,
                'generic': self.generic,
            })
            return {
                'name': 'Stat regex',
                'type': 'ir.actions.act_window',
                'res_model': 'runbot.build.stat.regex',
                'view_mode': 'form',
                'res_id': stat_regex.id
            }
