import logging
import re

from odoo import models, fields

_logger = logging.getLogger(__name__)
class Project(models.Model):
    _name = _description = 'runbot_merge.project'

    name = fields.Char(required=True, index=True)
    repo_ids = fields.One2many(
        'runbot_merge.repository', 'project_id',
        help="Repos included in that project, they'll be staged together. "\
        "*Not* to be used for cross-repo dependencies (that is to be handled by the CI)"
    )
    branch_ids = fields.One2many(
        'runbot_merge.branch', 'project_id',
        context={'active_test': False},
        help="Branches of all project's repos which are managed by the merge bot. Also "\
        "target branches of PR this project handles."
    )

    ci_timeout = fields.Integer(
        default=60, required=True,
        help="Delay (in minutes) before a staging is considered timed out and failed"
    )

    github_token = fields.Char("Github Token", required=True)
    github_prefix = fields.Char(
        required=True,
        default="hanson", # mergebot du bot du bot du~
        help="Prefix (~bot name) used when sending commands from PR "
             "comments e.g. [hanson retry] or [hanson r+ p=1]"
    )

    batch_limit = fields.Integer(
        default=8, help="Maximum number of PRs staged together")

    secret = fields.Char(
        help="Webhook secret. If set, will be checked against the signature "
             "of (valid) incoming webhook signatures, failing signatures "
             "will lead to webhook rejection. Should only use ASCII."
    )

    def _check_stagings(self, commit=False):
        for branch in self.search([]).mapped('branch_ids').filtered('active'):
            staging = branch.active_staging_id
            if not staging:
                continue
            try:
                with self.env.cr.savepoint():
                    staging.check_status()
            except Exception:
                _logger.exception("Failed to check staging for branch %r (staging %s)",
                                  branch.name, staging)
            else:
                if commit:
                    self.env.cr.commit()

    def _create_stagings(self, commit=False):
        for branch in self.search([]).mapped('branch_ids').filtered('active'):
            if not branch.active_staging_id:
                try:
                    with self.env.cr.savepoint():
                        branch.try_staging()
                except Exception:
                    _logger.exception("Failed to create staging for branch %r", branch.name)
                else:
                    if commit:
                        self.env.cr.commit()

    def _find_commands(self, comment):
        return re.findall(
            '^\s*[@|#]?{}:? (.*)$'.format(self.github_prefix),
            comment, re.MULTILINE | re.IGNORECASE)

    def _has_branch(self, name):
        self.env.cr.execute("""
        SELECT 1 FROM runbot_merge_branch
        WHERE project_id = %s AND name = %s
        LIMIT 1
        """, (self.id, name))
        return bool(self.env.cr.rowcount)
