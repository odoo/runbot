from odoo.tests.common import TransactionCase, Like

from odoo.addons.runbot.common import pseudo_markdown


class TestUtils(TransactionCase):

    def test_md_formatting(self):
        self.assertEqual(
            pseudo_markdown(
                "**strong** ~~delete~~ __italic__ \n"
            ),
            "<strong>strong</strong> "
            "<del>delete</del> "
            "<ins>italic</ins> "
            "<br/>\n"
        )

    def test_md_icons(self):
        self.assertEqual(
            pseudo_markdown(
                "@icon-star"
            ),
            '<i class="fa fa-star"></i>'
        )

    def test_md_urls(self):
        # Basic
        self.assertEqual(
            pseudo_markdown(
                "[name](https://runbot.odoo.com)"
            ),
            '<a href="https://runbot.odoo.com">name</a>'
        )
        # Test with target
        self.assertEqual(
            pseudo_markdown(
                "[name](https://runbot.odoo.com fp)"
            ),
            '<a href="https://runbot.odoo.com" target="fp">name</a>'
        )

        # Everything at once
        self.assertEqual(
            pseudo_markdown(
                "[@icon-star](https://runbot.odoo.com _blank)"
            ),
            '<a href="https://runbot.odoo.com" target="_blank"><i class="fa fa-star"></i></a>'
        )
