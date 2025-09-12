import logging
import re
from typing import List

import requests
import sentry_sdk

from odoo import models, fields, api
from odoo.fields import Domain
from odoo.tools import reverse_order

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
    staging_enabled = fields.Boolean(default=True)
    staging_priority = fields.Selection([
        ('default', "Splits over ready PRs"),
        ('largest', "Largest of split and ready PRs"),
        ('ready', "Ready PRs over split"),
    ], default="default", required=True)
    staging_statuses = fields.Boolean(default=True)
    staging_rpc = fields.Boolean(default=False)

    ci_timeout = fields.Integer(
        default=60, required=True, group_operator=None,
        help="Delay (in minutes) before a staging is considered timed out and failed"
    )

    github_token = fields.Char("Github Token", required=True)
    github_name = fields.Char(store=True, compute="_compute_identity", required=True, copy=True)
    github_email = fields.Char(store=True, compute="_compute_identity", required=True, copy=True)
    github_prefix = fields.Char(
        required=True,
        default="hanson", # mergebot du bot du bot du~
        help="Prefix (~bot name) used when sending commands from PR "
             "comments e.g. [hanson retry] or [hanson r+ priority]",
    )
    fp_github_token = fields.Char()
    fp_github_name = fields.Char(store=True, compute="_compute_git_identity")

    batch_limit = fields.Integer(
        default=8, group_operator=None, help="Maximum number of PRs staged together")

    freeze_id = fields.Many2one('runbot_merge.project.freeze', compute='_compute_freeze')
    freeze_reminder = fields.Text()

    uniquifier = fields.Boolean(
        default=True,
        help="Whether to add a uniquifier commit on repositories without PRs"
             " during staging. The lack of uniquifier can lead to CI conflicts"
             " as github works off of commits, so it's possible for an"
             " unrelated build to trigger a failure if somebody is a dummy and"
             " includes repos they have no commit for."
    )

    @api.depends('github_token')
    def _compute_identity(self):
        s = requests.Session()
        for project in self:
            if not project.github_token or (project.github_name and project.github_email):
                continue

            headers = {'Authorization': f'token {project.github_token}'}
            r0 = s.get('https://api.github.com/user', headers=headers)
            if not r0.ok:
                _logger.warning("Failed to fetch merge bot information for project %s: %s", project.name, r0.text or r0.content)
                continue

            r = r0.json()
            project.github_name = r['name'] or r['login']
            if email := r['email']:
                project.github_email = email
                continue

            if 'user:email' not in set(re.split(r',\s*', r0.headers['x-oauth-scopes'])):
                _logger.warning("Unable to fetch merge bot emails for project %s: scope missing from token", project.name)
            r1 = s.get('https://api.github.com/user/emails', headers=headers)
            if not r1.ok:
                _logger.warning("Failed to fetch merge bot emails for project %s: %s", project.name, r1.text or r1.content)
                continue

            project.github_email = next((
                entry['email']
                for entry in r1.json()
                if entry['primary']
            ), None)

    # technically the email could change at any moment...
    @api.depends('fp_github_token')
    def _compute_git_identity(self):
        s = requests.Session()
        for project in self:
            if project.fp_github_name or not project.fp_github_token:
                continue

            r0 = s.get('https://api.github.com/user', headers={
                'Authorization': 'token %s' % project.fp_github_token
            })
            if not r0.ok:
                _logger.error("Failed to fetch forward bot information for project %s: %s", project.name, r0.text or r0.content)
                continue

            user = r0.json()
            project.fp_github_name = user['name'] or user['login']

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
            ('project_id.staging_enabled', '=', True),
        ]):
            try:
                with self.env.cr.savepoint():
                    if not self.env['runbot_merge.patch']._apply_patches(branch):
                        self.env.ref("runbot_merge.staging_cron")._trigger()
                        return

            except Exception:
                _logger.exception("Failed to apply patches to branch %r", branch.name)
            else:
                if commit:
                    self.env.cr.commit()

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

    def _find_commands(self, comment: str) -> List[str]:
        """Tries to find all the lines starting (ignoring leading whitespace)
        with either the merge or the forward port bot identifiers.

        For convenience, the identifier *can* be prefixed with an ``@`` or
        ``#``, and suffixed with a ``:``.
        """
        # horizontal whitespace (\s - {\n, \r}), but Python doesn't have \h or \p{Blank}
        h = r'[^\S\r\n]'
        return re.findall(
            fr'^{h}*[@|#]?{self.github_prefix}(?:{h}+|:{h}*)(.*)$',
            comment, re.MULTILINE | re.IGNORECASE)

    def _has_branch(self, name):
        self.env['runbot_merge.branch'].flush_model(['project_id', 'name'])
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

    def _forward_port_ordered(self, domain=()):
        Branches = self.env['runbot_merge.branch']
        return Branches.search(Domain.AND([
            [('project_id', '=', self.id)],
            domain or [],
        ]), order=reverse_order(Branches._order))
