# -*- coding: utf-8 -*-
from .common import RunbotCase


class TestIrLogging(RunbotCase):

    def simulate_log(self, build, func, message, level='INFO'):
        """ simulate ir_logging from an external build """
        dest = '%s-fake-dest' % build.id
        val = ('server', dest, 'test', level, message, 'test', '0', func)
        self.cr.execute("""
                INSERT INTO ir_logging(create_date, type, dbname, name, level, message, path, line, func)
                VALUES (NOW() at time zone 'UTC', %s, %s, %s, %s, %s, %s, %s, %s)
            """, val)

    def test_ir_logging(self):
        build = self.Build.create({
            'active_step': self.env.ref('runbot.runbot_build_config_step_test_all').id,
            'params_id': self.base_params.id,
        })

        build.log_counter = 10

        #  Test that an ir_logging is created and a the trigger set the build_id
        self.simulate_log(build, 'test function', 'test message')
        log_line = self.env['ir.logging'].search([('func', '=', 'test function'), ('message', '=', 'test message'), ('level', '=', 'INFO')])
        self.assertEqual(len(log_line), 1, "A build log event should have been created")
        self.assertEqual(log_line.build_id, build)
        self.assertEqual(log_line.active_step_id, self.env.ref('runbot.runbot_build_config_step_test_all'), 'The active step should be set on the log line')

        #  Test that a warn log line sets the build in warn
        self.simulate_log(build, 'test function', 'test message', level='WARNING')
        build.invalidate_cache()
        self.assertEqual(build.triggered_result, 'warn', 'A warning log should sets the build in warn')

        #  Test that a error log line sets the build in ko
        self.simulate_log(build, 'test function', 'test message', level='ERROR')
        build.invalidate_cache()
        self.assertEqual(build.triggered_result, 'ko', 'An error log should sets the build in ko')
        self.assertEqual(7, build.log_counter, 'server lines should decrement the build log_counter')

        build.log_counter = 10

        # Test the log limit
        for i in range(11):
            self.simulate_log(build, 'limit function', 'limit message')
        log_lines = self.env['ir.logging'].search([('build_id', '=', build.id), ('type', '=', 'server'), ('func', '=', 'limit function'), ('message', '=', 'limit message'), ('level', '=', 'INFO')])
        self.assertGreater(len(log_lines), 7, 'Trigger should have created logs with appropriate build id')
        self.assertLess(len(log_lines), 10, 'Trigger should prevent insert more lines of logs than log_counter')
        last_log_line = self.env['ir.logging'].search([('build_id', '=', build.id)], order='id DESC', limit=1)
        self.assertIn('Log limit reached', last_log_line.message, 'Trigger should modify last log message')

        # Test that the _log method is still able to add logs
        build._log('runbot function', 'runbot message')
        log_lines = self.env['ir.logging'].search([('type', '=', 'runbot'), ('name', '=', 'odoo.runbot'), ('func', '=', 'runbot function'), ('message', '=', 'runbot message'), ('level', '=', 'INFO')])
        self.assertEqual(len(log_lines), 1, '_log should be able to add logs from the runbot')

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
            'foo &lt;script&gt;console.log(&quot;hello world&quot;)&lt;/script&gt;'
        )
