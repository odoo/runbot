from odoo import models, fields


class CodeownerWizard(models.TransientModel):
    _name = 'runbot.codeowner.wizard'
    _description = 'Codeowner Test Wizard'

    file_names = fields.Text('File Names', required=True, help='Enter one file name per line')
    project_id = fields.Many2one('runbot.project', string='Project', required=True, default=lambda self: self.env.ref('runbot.main_project'))
    version_id = fields.Many2one('runbot.version', string='Version')
    repo_id = fields.Many2one('runbot.repo', string='Repository', required=True, domain="[('project_id', '=', project_id)]")
    result_ids = fields.One2many('runbot.codeowner.wizard.result', 'wizard_id', string='Results')

    def _return_to_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_test(self):
        self.result_ids.unlink()
        if not self.file_names:
            return self._return_to_wizard()

        files = [f.strip() for f in self.file_names.splitlines() if f.strip()]
        if not files:
            return self._return_to_wizard()

        step = self.env['runbot.build.config.step'].search([('job_type', '=', 'codeowner')], limit=1)
        if not step:
            return self._return_to_wizard()

        codeowners = self.env['runbot.codeowner'].search([('project_id', '=', self.project_id.id)])
        regexes = step._codeowners_regexes(codeowners, self.version_id or self.env['runbot.version'].browse())
        ownerships = self.env['runbot.module.ownership'].search([('team_id.github_team', '!=', False)])

        reviewer_per_file, reasons_per_file = step._reviewer_per_file(
            files, regexes, ownerships, self.repo_id
        )

        results = []
        for file in files:
            reviewers = reviewer_per_file.get(file, set())
            reasons = reasons_per_file.get(file, {})

            reason_parts = []
            for team, team_reasons in sorted(reasons.items()):
                for reason in team_reasons:
                    if reason['type'] == 'regex':
                        reason_parts.append('%s: codeowner rule' % team)
                    elif reason['type'] == 'module':
                        reason_parts.append('%s: module ownership (%s)' % (team, reason['ownership_id'].module_id.name))
                    elif reason['type'] == 'fallback_module':
                        reason_parts.append('%s: fallback module ownership (%s)' % (team, reason['ownership_id'].module_id.name))
                    elif reason['type'] == 'fallback_reviewer':
                        reason_parts.append('%s: fallback reviewer' % team)

            results.append((0, 0, {
                'filename': file,
                'reviewers': ', '.join(sorted(reviewers)) if reviewers else 'No reviewer',
                'reasons': '\n'.join(reason_parts) if reason_parts else '-',
            }))

        self.result_ids = results
        return self._return_to_wizard()


class CodeownerWizardResult(models.TransientModel):
    _name = 'runbot.codeowner.wizard.result'
    _description = 'Codeowner Test Wizard Result'

    wizard_id = fields.Many2one('runbot.codeowner.wizard', required=True, ondelete='cascade')
    filename = fields.Char('File', readonly=True)
    reviewers = fields.Char('GitHub Teams', readonly=True)
    reasons = fields.Text('Reasons', readonly=True)
