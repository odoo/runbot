from .test_build_config_step import TestBuildConfigStepCommon


class TestReviewerPerFile(TestBuildConfigStepCommon):
    def setUp(self):
        super().setUp()
        self.config_step = self.ConfigStep.create({
            'name': 'test_codeowner',
            'job_type': 'codeowner',
            'fallback_reviewer': 'fallback-team',
        })
        self.accounting_team = self.env['runbot.team'].create({'name': 'AccountingTestTeam', 'github_team': 'test_accounting_team'})
        self.discuss_team = self.env['runbot.team'].create({'name': 'DiscussTestTeam', 'github_team': 'test_discuss_team'})
        self.framework_team = self.env['runbot.team'].create({'name': 'FrameworkTestTeam', 'github_team': 'framework_test_team'})

        self.module_a = self.env['runbot.module'].create({'name': 'module_a'})

        self.no_codeowners = self.env['runbot.codeowner'].browse()
        self.v19 = self.Version._get('19.0')

    def test_regex_single_match(self):
        codeowner = self.env['runbot.codeowner'].create({
            'project_id': self.project.id,
            'regex': r' (odoo(/odoo)?/addons|enterprise)/accounting/.*',
            'github_teams': 'test_accounting_team',
        })
        codeowners = self.env['runbot.codeowner'].search([('project_id', '=', self.project.id)])
        regexes = self.config_step._codeowners_regexes(codeowners, self.v19)
        ownerships = self.env['runbot.module.ownership'].browse()
        file_path = 'odoo/addons/accounting/file.py'
        reviewer_per_file, reasons_per_file = self.config_step._reviewer_per_file([file_path], regexes, ownerships, self.repo_odoo)
        self.assertIn(file_path, reviewer_per_file)
        self.assertEqual(reviewer_per_file[file_path], {'test_accounting_team'})
        expected_reason = {'type': 'regex', 'codeowner_ids': codeowner}
        self.assertEqual(reasons_per_file[file_path]['test_accounting_team'][0], expected_reason)

    def test_regex_multiple_teams_match(self):
        co_accounting = self.env['runbot.codeowner'].create({
            'project_id': self.project.id,
            'regex': r' (odoo(/odoo)?/addons|enterprise)/accounting/.*',
            'github_teams': 'test_accounting_team',
        })

        self.env['runbot.codeowner'].create({
            'project_id': self.project.id,
            'regex': r' (odoo(/odoo)?/addons|enterprise)/mail/.*\.js',
            'github_teams': 'test_discuss_team',
        })

        co_framework = self.env['runbot.codeowner'].create({
            'project_id': self.project.id,
            'regex': r' (odoo(/odoo)?/addons|enterprise)/.*\.py',
            'github_teams': 'test_framework_team',
        })

        codeowners = self.env['runbot.codeowner'].search([('project_id', '=', self.project.id)])
        regexes = self.config_step._codeowners_regexes(codeowners, self.v19)
        ownerships = self.env['runbot.module.ownership'].browse()
        file_path = 'odoo/addons/accounting/file.py'
        reviewer_per_file, reasons_per_file = self.config_step._reviewer_per_file([file_path], regexes, ownerships, self.repo_odoo)
        self.assertEqual(reviewer_per_file[file_path], {'test_accounting_team', 'test_framework_team'})
        expected_reasons = {
            "test_accounting_team": [
                {
                    "type": "regex",
                    "codeowner_ids": co_accounting,
                },
            ],
            "test_framework_team": [
                {
                    "type": "regex",
                    "codeowner_ids": co_framework,
                },
            ],
        }
        self.assertEqual(reasons_per_file[file_path], expected_reasons)
