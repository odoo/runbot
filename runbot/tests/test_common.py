from odoo.tests.common import BaseCase

from ..common import ansi_to_html


class TestAnsiToHtml(BaseCase):

    def test_foreground_with_other_attributes(self):
        for attribute in (1, 4, 22, 24, 49):
            with self.subTest(attribute=attribute):
                self.assertEqual(
                    ansi_to_html(f'\x1b[31;{attribute}mred'),
                    '<span style="white-space:pre-wrap"><span style="color:#AA0000">red</span></span>',
                )
                self.assertEqual(
                    ansi_to_html(f'\x1b[31mred\x1b[{attribute}m text'),
                    '<span style="white-space:pre-wrap"><span style="color:#AA0000">red text</span></span>',
                )

    def test_foreground_reset(self):
        for reset in ('0', '39', ''):
            with self.subTest(reset=reset):
                self.assertEqual(
                    ansi_to_html(f'\x1b[31mred\x1b[{reset}mplain'),
                    '<span style="white-space:pre-wrap"><span style="color:#AA0000">red</span>plain</span>',
                )

    def test_foreground_change(self):
        self.assertEqual(
            ansi_to_html('\x1b[31mred\x1b[32mgreen'),
            '<span style="white-space:pre-wrap"><span style="color:#AA0000">red</span>'
            '<span style="color:#00AA00">green</span></span>',
        )
