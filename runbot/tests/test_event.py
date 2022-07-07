# -*- coding: utf-8 -*-
from .common import RunbotCase


class TestIrLogging(RunbotCase):

    def test_markdown(self):
        log = self.env['ir.logging'].create({
            'name': 'odoo.runbot',
            'type': 'runbot',
            'path': 'runbot',
            'level': 'INFO',
            'line': 0,
            'func': 'test_markdown',
            'message': 'some **bold text** and also some __underlined text__ and maybe a bit of ~~strikethrough text~~'
        })

        self.assertEqual(
            log._markdown(),
            'some <strong>bold text</strong> and also some <ins>underlined text</ins> and maybe a bit of <del>strikethrough text</del>'
        )

        #log.message = 'a bit of code `import foo\nfoo.bar`'
        #self.assertEqual(
        #    log._markdown(),
        #    'a bit of code <code>import foo\nfoo.bar</code>'
        #)

        log.message = 'a bit of code :\n`import foo`'
        self.assertEqual(
            log._markdown(),
            'a bit of code :<br/><code>import foo</code>'
        )


        # test icon
        log.message = 'Hello @icon-file-text-o'
        self.assertEqual(
            log._markdown(),
            'Hello <i class="fa fa-file-text-o"></i>'
        )

        log.message = 'a bit of code :\n`print(__name__)`'
        self.assertEqual(
            log._markdown(),
            'a bit of code :<br/><code>print(__name__)</code>'
        )

        log.message = 'a bit of __code__ :\n`print(__name__)` **but also** `print(__name__)`'
        self.assertEqual(
            log._markdown(),
            'a bit of <ins>code</ins> :<br/><code>print(__name__)</code> <strong>but also</strong> <code>print(__name__)</code>'
        )


        # test links
        log.message = 'This [link](https://wwww.somewhere.com) goes to somewhere and [this one](http://www.nowhere.com) to nowhere.'
        self.assertEqual(
            log._markdown(),
            'This <a href="https://wwww.somewhere.com">link</a> goes to somewhere and <a href="http://www.nowhere.com">this one</a> to nowhere.'
        )

        # test link with icon
        log.message = '[@icon-download](https://wwww.somewhere.com) goes to somewhere.'
        self.assertEqual(
            log._markdown(),
            '<a href="https://wwww.somewhere.com"><i class="fa fa-download"></i></a> goes to somewhere.'
        )

        # test links with icon and text
        log.message = 'This [link@icon-download](https://wwww.somewhere.com) goes to somewhere.'
        self.assertEqual(
            log._markdown(),
            'This <a href="https://wwww.somewhere.com">link<i class="fa fa-download"></i></a> goes to somewhere.'
        )

        # test sanitization
        log.message = 'foo <script>console.log("hello world")</script>'
        self.assertEqual(
            log._markdown(),
            'foo &lt;script&gt;console.log(&#34;hello world&#34;)&lt;/script&gt;'
        )
