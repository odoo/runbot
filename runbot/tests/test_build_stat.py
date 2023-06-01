# -*- coding: utf-8 -*-
from psycopg2 import IntegrityError
from unittest.mock import patch, mock_open
from odoo.exceptions import ValidationError
from odoo.tools import mute_logger
from .common import RunbotCase


class TestBuildStatRegex(RunbotCase):
    def setUp(self):
        super(TestBuildStatRegex, self).setUp()
        self.StatRegex = self.env["runbot.build.stat.regex"]
        self.ConfigStep = self.env["runbot.build.config.step"]
        self.BuildStat = self.env["runbot.build.stat"]
        self.Build = self.env["runbot.build"]

        params = self.BuildParameters.create({
            'version_id': self.version_13.id,
            'project_id': self.project.id,
            'config_id': self.default_config.id,
            'config_data': {'make_stats': True}
        })

        self.build = self.Build.create(
            {
                "params_id": params.id,
                "port": "1234",
            }
        )

        self.config_step = self.env["runbot.build.config.step"].create(
            {
                "name": "a_nice_step",
                "job_type": "install_odoo",
                "make_stats": True,
                "build_stat_regex_ids": [(0, 0, {"name": "query_count", "regex":  r"odoo.addons.(?P<key>.+) tested in .+, (?P<value>\d+) queries", "generic": False})]
            }
        )

    def test_build_stat_regex_validation(self):

        #  test that a regex without a named key 'value' raises a ValidationError
        with self.assertRaises(ValidationError):
            self.StatRegex.create(
                {"name": "query_count", "regex": "All post-tested in .+s, .+ queries"}
            )

    def test_build_stat_regex_find_in_file(self):

        max_id = self.BuildStat.search([], order="id desc", limit=1).id or 0
        file_content = """foo bar
2020-03-02 22:06:58,391 17 INFO xxx odoo.modules.module: odoo.addons.website_blog.tests.test_ui tested in 10.35s, 2501 queries
some garbage
2020-03-02 22:07:14,340 17 INFO xxx odoo.modules.module: odoo.addons.website_event.tests.test_ui tested in 9.26s, 2435 queries
nothing to see here
"""
        self.start_patcher(
            "isdir", "odoo.addons.runbot.models.build_stat_regex.os.path.exists", True
        )
        with patch("builtins.open", mock_open(read_data=file_content)):
            self.config_step._make_stats(self.build)
        self.assertEqual(
            dict(self.BuildStat.search([('category', '=', 'query_count'), ('id', '>', max_id)]).values), 
            {
                'website_event.tests.test_ui': 2435.0,
                'website_blog.tests.test_ui': 2501.0
            }
        )

        # Check unicity
        with self.assertRaises(IntegrityError):
            with mute_logger("odoo.sql_db"):
                with self.cr.savepoint():  # needed to continue tests
                    self.env["runbot.build.stat"].create({
                        'build_id': self.build.id, 
                        'config_step_id': self.config_step.id,
                        'category': 'query_count',
                        'values': {'website_event.tests.test_ui': 2435},
                        }

                    )

    def test_build_stat_regex_generic(self):
        """ test that regex are not used when generic is False and that _make_stats use all genreic regex if there are no regex on step """
        max_id = self.BuildStat.search([], order="id desc", limit=1).id or 0
        file_content = """foo bar
odoo.addons.foobar tested in 2s, 25 queries
useless 10
chocolate 15
"""

        self.config_step.build_stat_regex_ids = False

        # this one is not generic and thus should not be used
        self.StatRegex.create({"name": "useless_count", "regex":  r"(?P<key>useless) (?P<value>\d+)", "generic": False})

        # this is one is the only one that should be used
        self.StatRegex.create({"name": "chocolate_count", "regex":  r"(?P<key>chocolate) (?P<value>\d+)"})

        self.start_patcher(
            "isdir", "odoo.addons.runbot.models.build_stat_regex.os.path.exists", True
        )
        with patch("builtins.open", mock_open(read_data=file_content)):
            self.config_step._make_stats(self.build)

        self.assertEqual(self.BuildStat.search_count([('category', '=', 'query_count'), ('id', '>', max_id)]), 0)
        self.assertEqual(self.BuildStat.search_count([('category', '=', 'useless_count'), ('id', '>', max_id)]), 0)
        self.assertEqual(dict(self.BuildStat.search([('category', '=', 'chocolate_count'), ('id', '>', max_id)]).values), {'chocolate': 15.0})

    def test_build_stat_regex_find_in_file_perf(self):
        max_id = self.BuildStat.search([], order="id desc", limit=1).id or 0
        noise_lines = """2020-03-17 13:26:15,472 2376 INFO runbottest odoo.modules.loading: loading runbot/views/build_views.xml
2020-03-10 22:58:34,472 17 INFO 1709329-master-9938b2-all_no_autotag werkzeug: 127.0.0.1 - - [10/Mar/2020 22:58:34] "POST /mail/read_followers HTTP/1.1" 200 - 13 0.004 0.009
2020-03-10 22:58:30,137 17 INFO ? werkzeug: 127.0.0.1 - - [10/Mar/2020 22:58:30] "GET /website/static/src/xml/website.editor.xml HTTP/1.1" 200 - - - -
"""

        match_lines = [
            "2020-03-02 22:06:58,391 17 INFO xxx odoo.modules.module: odoo.addons.website_blog.tests.test_ui tested in 10.35s, 2501 queries",
            "2020-03-02 22:07:14,340 17 INFO xxx odoo.modules.module: odoo.addons.website_event.tests.test_ui tested in 9.26s, 2435 queries"
        ]

        # generate a 13 MiB log file with two potential matches
        log_data = ""
        for l in match_lines:
            log_data += noise_lines * 10000
            log_data += l
        log_data += noise_lines * 10000

        self.start_patcher(
            "isdir", "odoo.addons.runbot.models.build_stat_regex.os.path.exists", True
        )
        with patch("builtins.open", mock_open(read_data=log_data)):
            self.config_step._make_stats(self.build)

        self.assertEqual(
            dict(self.BuildStat.search([('category', '=', 'query_count'), ('id', '>', max_id)]).values), 
            {
                'website_event.tests.test_ui': 2435.0,
                'website_blog.tests.test_ui': 2501.0
            }
        )
