import logging
import re
import shutil
from typing import List

import requests

from odoo import models, fields, api
from odoo.exceptions import UserError
from odoo.osv import expression
from odoo.tools import reverse_order, groupby

_logger = logging.getLogger(__name__)
class Project(models.Model):
    _name = _description = 'runbot_merge.project'

    active = fields.Boolean(default=True)
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
    staging_pattern = fields.Char(
        required=True,
        default="%(stage)s.%(target)s%(sub)s",
        help="""\
Pattern for the naming of the staging branches. Editing this will switch over
every staging branch instantaneously, so any existing staging will be extremely
confused (and will probably timeout).

- `stage` is either tmp or staging, it must be either at the start or end of the pattern
- `target` is the target branch
- `sub` is the sub-staging, used for presplits and optimistic secondary stagings
"""
    )
    staging_sub_pattern = fields.Char(required=True, default=".%(sub)s")
    staging_priority = fields.Selection([
        ('default', "Splits over ready PRs"),
        ('largest', "Largest of split and ready PRs"),
        ('ready', "Ready PRs over split"),
    ], default="default", required=True)
    staging_statuses = fields.Boolean(default=True)

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
    lateness_limit = fields.Integer(
        help="If set, how far back a PR can be and still be staged"
             " (default=always staged), on large project when a PR is too old"
             " it's increasingly likely that it will have logical (even if not"
             " physical) conflicts with the target causing staging failures."
             "\n\n"
             "WARNING: because in Odoo null ~ 0, the comparison is done via `<`"
             " to support a strict \"PR must be up to date\" policy.",
    )

    freeze_id = fields.Many2one('runbot_merge.project.freeze', compute='_compute_freeze')
    freeze_reminder = fields.Text()

    uniquifier = fields.Boolean(
        default=False,
        help="Whether to add a uniquifier commit on repositories without PRs"
             " during staging. The lack of uniquifier can lead to CI conflicts"
             " as github works off of commits, so it's possible for an"
             " unrelated build to trigger a failure if somebody is a dummy and"
             " includes repos they have no commit for."
    )

    use_mergiraf = fields.Boolean(help="Use mergiraf as merge driver")
    warn_mergiraf = fields.Boolean(compute='_compute_warn_mergiraf')

    fw_nice = fields.Boolean(help="Lower priority of forward-ports")
    disable_forwardport = fields.Boolean(help="If checked, forward port will be disabled for this project.")
    request_missing_statuses = fields.Boolean(
        help="If some required statuses have never been received on a PR,"
             " request them from the runbot."
    )

    @api.depends('use_mergiraf')
    def _compute_warn_mergiraf(self):
        for project in self:
            project.warn_mergiraf = \
                project.use_mergiraf and not shutil.which('mergiraf')

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
            ('project_id.active', '=', True),
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
                with self.env.cr.savepoint():
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
        return Branches.search(expression.AND([
            [('project_id', '=', self.id)],
            domain or [],
        ]), order=reverse_order(Branches._order))

    def write(self, vals):
        if ((vals.get('staging_enabled') is True and not all(self.mapped('staging_enabled')))
         or (vals.get('active') is True and not all(self.mapped('active')))
        ):
            self.env.ref('runbot_merge.staging_cron')._trigger()
        # projects without an fw token can't have forward ports, thus don't need
        # intermediates or followups being checked for
        if fw_enabled := self.filtered('fp_github_token').with_context(active_test=False):
            # check on branches both active and inactive so disabling branches doesn't
            # make it look like the sequence changed.
            previously_active_branches = {project: project.branch_ids.filtered('active') for project in fw_enabled}
            branches_before = {project: project._forward_port_ordered() for project in fw_enabled}

            r = super().write(vals)
            fw_enabled._followup_prs(previously_active_branches)
            fw_enabled._insert_intermediate_prs(branches_before)
            return r
        return super().write(vals)

    def _followup_prs(self, previously_active_branches):
        """If a branch has been disabled and had PRs without a followup (e.g.
        because no CI or CI failed), create followup, as if the branch had been
        originally disabled (and thus skipped over)
        """
        Batch = self.env['runbot_merge.batch']
        ported = self.env['runbot_merge.pull_requests']
        for p in self:
            actives = previously_active_branches[p]
            for deactivated in p.branch_ids.filtered(lambda b: not b.active) & actives:
                # if a non-merged batch targets a deactivated branch which is
                # not its limit
                extant = Batch.search([
                    ('parent_id', '!=', False),
                    ('target', '=', deactivated.id),
                    # if at least one of the PRs has a different limit
                    ('prs.limit_id', '!=', deactivated.id),
                    ('merge_date', '=', False),
                ]).filtered(lambda b:\
                    # and has a next target (should already be a function of
                    # the search but doesn't hurt)
                    b._find_next_target() \
                    # and has not already been forward ported
                    and Batch.search_count([('parent_id', '=', b.id)]) == 0
                )

                # PRs may have different limits in the same batch so only notify
                # those which actually needed porting
                ported |= extant._schedule_fp_followup(force_fw=True)\
                    .prs.filtered(lambda p: p._find_next_target())

        if not ported:
            return

        for feedback in self.env['runbot_merge.pull_requests.feedback'].search(expression.OR(
            [('repository', '=', p.repository.id), ('pull_request', '=', p.number)]
            for p in ported
        )):
            # FIXME: better signal
            if 'disabled' in feedback.message:
                feedback.message += '\n\nAs this was not its limit, it will automatically be forward ported to the next active branch.'

    def _insert_intermediate_prs(self, branches_before):
        """If new branches have been added to the sequence inbetween existing
        branches (mostly a freeze inserted before the main branch), fill in
        forward-ports for existing sequences
        """
        Branches = self.env['runbot_merge.branch']
        for p in self:
            # check if the branches sequence has been modified
            bbefore = branches_before[p]
            if not bbefore:
                continue

            bafter = p._forward_port_ordered()
            if not bbefore <= bafter:
                raise UserError("Branches can not be removed after saving the project.")
            # branches inserted at the end is fine, forwardports will keep on keeping normally
            if all(before == after for before, after in zip(bbefore, bafter)):
                continue

            logger = _logger.getChild('project').getChild(p.name)
            logger.debug("branches updated %s -> %s", bbefore, bafter)

            # Last possibility: branch was inserted but not at end, get all
            # branches before and all branches after
            before = new = after = Branches
            for b in bafter:
                if b in bbefore:
                    if new:
                        after += b
                    else:
                        before += b
                else:
                    if new:
                        raise UserError("Inserting multiple branches at the same time is not supported")
                    new = b
            if not before:
                continue

            logger.debug('before: %s new: %s after: %s', before.ids, new.ids, after.ids)
            # find all FPs whose ancestry spans the insertion
            leaves = self.env['runbot_merge.pull_requests'].search([
                ('state', 'not in', ['closed', 'merged']),
                ('target', 'in', after.ids),
                ('source_id.target', 'in', before.ids),
            ])
            # get all PRs just preceding the insertion point which either are
            # sources of the above or have the same source
            candidates = self.env['runbot_merge.pull_requests'].search([
                ('target', '=', before[-1].id),
                '|', ('id', 'in', leaves.mapped('source_id').ids),
                     ('source_id', 'in', leaves.mapped('source_id').ids),
            ])
            logger.debug("\nPRs spanning new: %s\nto port: %s", leaves, candidates)
            # enqueue the creation of a new forward-port based on our candidates
            # but it should only create a single step and needs to stitch back
            # the parents linked list, so it has a special type
            for _, cs in groupby(candidates, key=lambda p: p.label):
                self.env['forwardport.batches'].create({
                    'batch_id': cs[0].batch_id.id,
                    'source': 'insert',
                })
