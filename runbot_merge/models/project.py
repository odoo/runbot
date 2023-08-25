import logging
import re

import requests
import sentry_sdk

from odoo import models, fields, api
from odoo.exceptions import UserError

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
        default=60, required=True, group_operator=None,
        help="Delay (in minutes) before a staging is considered timed out and failed"
    )

    github_token = fields.Char("Github Token", required=True)
    github_name = fields.Char(store=True, compute="_compute_identity")
    github_email = fields.Char(store=True, compute="_compute_identity")
    github_prefix = fields.Char(
        required=True,
        default="hanson", # mergebot du bot du bot du~
        help="Prefix (~bot name) used when sending commands from PR "
             "comments e.g. [hanson retry] or [hanson r+ p=1]",
    )

    batch_limit = fields.Integer(
        default=8, group_operator=None, help="Maximum number of PRs staged together")

    secret = fields.Char(
        help="Webhook secret. If set, will be checked against the signature "
             "of (valid) incoming webhook signatures, failing signatures "
             "will lead to webhook rejection. Should only use ASCII."
    )

    freeze_id = fields.Many2one('runbot_merge.project.freeze', compute='_compute_freeze')
    freeze_reminder = fields.Text()

    @api.depends('github_token')
    def _compute_identity(self):
        s = requests.Session()
        for project in self:
            if not project.github_token or (project.github_name and project.github_email):
                continue

            r0 = s.get('https://api.github.com/user', headers={
                'Authorization': 'token %s' % project.github_token
            })
            if not r0.ok:
                _logger.error("Failed to fetch merge bot information for project %s: %s", project.name, r0.text or r0.content)
                continue

            r0 = r0.json()
            project.github_name = r0['name'] or r0['login']
            if email := r0['email']:
                project.github_email = email
                continue

            if 'user:email' not in set(re.split(r',\s*', r0.headers['x-oauth-scopes'])):
                raise UserError("The merge bot github token needs the user:email scope to fetch the bot's identity.")
            r1 = s.get('https://api.github.com/user/emails', headers={
                'Authorization': 'token %s' % project.github_token
            })
            if not r1.ok:
                _logger.error("Failed to fetch merge bot emails for project %s: %s", project.name, r1.text or r1.content)
                continue
            project.github_email = next((
                entry['email']
                for entry in r1.json()
                if entry['primary']
            ), None)
            if not project.github_email:
                raise UserError("The merge bot needs a public or accessible primary email set up.")

    def _check_stagings(self, commit=False):
        # check branches with an active staging
        for branch in self.env['runbot_merge.branch']\
                .with_context(active_test=False)\
                .search([('active_staging_id', '!=', False)]):
            staging = branch.active_staging_id
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
        from .stagings_create import try_staging

        # look up branches which can be staged on and have no active staging
        for branch in self.env['runbot_merge.branch'].search([
            ('active_staging_id', '=', False),
            ('active', '=', True),
            ('staging_enabled', '=', True),
        ]):
            try:
                with self.env.cr.savepoint(), \
                    sentry_sdk.start_span(description=f'create staging {branch.name}') as span:
                    span.set_tag('branch', branch.name)
                    try_staging(branch)
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

    def _next_freeze(self):
        prev = self.branch_ids[1:2].name
        if not prev:
            return None

        m = re.search(r'(\d+)(?:\.(\d+))?$', prev)
        if m:
            return "%s.%d" % (m[1], (int(m[2] or 0) + 1))
        else:
            return f'post-{prev}'

    def _compute_freeze(self):
        freezes = {
            f.project_id.id: f.id
            for f in self.env['runbot_merge.project.freeze'].search([('project_id', 'in', self.ids)])
        }
        for project in self:
            project.freeze_id = freezes.get(project.id) or False

    def action_prepare_freeze(self):
        """ Initialises the freeze wizard and returns the corresponding action.
        """
        self.check_access_rights('write')
        self.check_access_rule('write')
        Freeze = self.env['runbot_merge.project.freeze'].sudo()

        w = Freeze.search([('project_id', '=', self.id)]) or Freeze.create({
            'project_id': self.id,
            'branch_name': self._next_freeze(),
            'release_pr_ids': [
                (0, 0, {'repository_id': repo.id})
                for repo in self.repo_ids
                if repo.freeze
            ]
        })
        return w.action_open()
