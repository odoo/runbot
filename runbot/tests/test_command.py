# -*- coding: utf-8 -*-
from odoo.tests import common
from ..container import Command


CONFIG = """[options]
foo = bar
"""


class Test_Command(common.TransactionCase):

    def test_command(self):
        pres = ['pip3', 'install', 'foo']
        posts = ['python3', '-m', 'coverage', 'html']
        finals = ['pgdump bar']
        cmd = Command([pres], ['python3', 'odoo-bin'], [posts], finals=[finals])
        self.assertEqual(str(cmd), 'python3 odoo-bin')

        expected = 'pip3 install foo && python3 odoo-bin && python3 -m coverage html ; pgdump bar'
        self.assertEqual(cmd.build(), expected)

        cmd = Command([pres], ['python3', 'odoo-bin'], [posts])
        cmd.add_config_tuple('a', 'b')
        cmd += ['bar']
        self.assertIn('bar', cmd.cmd)
        cmd.add_config_tuple('x', 'y')

        content = cmd.get_config(starting_config=CONFIG)

        self.assertIn('[options]', content)
        self.assertIn('foo = bar', content)
        self.assertIn('a = b', content)
        self.assertIn('x = y', content)

        with self.assertRaises(AssertionError):
            cmd.add_config_tuple('http-interface', '127.0.0.1')
