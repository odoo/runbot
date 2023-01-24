import contextlib
import enum
import itertools
import json
import logging
import time
from collections import Counter

from markupsafe import Markup

from odoo import models, fields, api, Command
from odoo.exceptions import UserError
from odoo.addons.runbot_merge.exceptions import FastForwardError

_logger = logging.getLogger(__name__)
class FreezeWizard(models.Model):
    _name = 'runbot_merge.project.freeze'
    _description = "Wizard for freezing a project('s master)"

    project_id = fields.Many2one('runbot_merge.project', required=True)
    errors = fields.Text(compute='_compute_errors')
    branch_name = fields.Char(required=True, help="Name of the new branches to create")

    required_pr_ids = fields.Many2many(
        'runbot_merge.pull_requests', string="Required Pull Requests",
        domain="[('state', 'not in', ('closed', 'merged'))]",
        help="Pull requests which must have been merged before the freeze is allowed",
    )

    pr_state_key = fields.Html(string="Color Key", compute='_compute_state_key', readonly=True)
    def _compute_state_key(self):
        s = dict(self.env['runbot_merge.pull_requests']._fields['state'].selection)
        self.pr_state_key = Markup("""
            <p>%s</p>
        """) % Markup(" ").join(
            Markup('<span class="badge border-0 fucking_color_key_{}">{}</span>').format(v, s[k])
            for k, v in STATE_COLORMAP.items()
            if v
        )

    release_label = fields.Char(
        string="Find by label",
        help="Setting a (complete) PR label will automatically find and "
             "configure the corresponding PRs as release",
    )
    release_pr_ids = fields.One2many(
        'runbot_merge.project.freeze.prs', 'wizard_id',
        string="Release pull requests",
        help="Pull requests used as tips for the freeze branches, "
             "one per repository",
    )

    bump_label = fields.Char(
        string="Find by label",
        help="Setting a (complete) PR label will automatically find and "
             "configure the corresponding PRs as bump",
    )
    bump_pr_ids = fields.One2many(
        'runbot_merge.project.freeze.bumps', 'wizard_id',
        string="Bump pull requests",
        help="Pull requests used as tips of the frozen-off branches, "
             "one per repository",
    )

    _sql_constraints = [
        ('unique_per_project', 'unique (project_id)',
         "There should be only one ongoing freeze per project"),
    ]

    @api.onchange('release_label')
    def _onchange_release_label(self):
        if not self.release_label:
            return

        prs = self.env['runbot_merge.pull_requests'].search([
            ('label', '=', self.release_label)
        ])
        for release_pr in self.release_pr_ids:
            release_pr.pr_id = prs.filtered(lambda p: p.repository == release_pr.repository_id)

    @api.onchange('release_pr_ids')
    def _onchange_release_prs(self):
        labels = {p.pr_id.label for p in self.release_pr_ids if p.pr_id}
        self.release_label = len(labels) == 1 and labels.pop()

    @api.onchange('bump_label')
    def _onchange_bump_label(self):
        if not self.release_label:
            return

        prs = self.env['runbot_merge.pull_requests'].search([
            ('label', '=', self.bump_label)
        ])

        commands = [Command.clear()]
        for pr in prs:
            current = self.bump_pr_ids.filtered(lambda bump_pr: bump_pr.repository_id == pr.repository)
            if current:
                commands.append(Command.update(current.id, {'pr_id': pr.id}))
            else:
                commands.append(Command.create({
                    'repository_id': pr.repository.id,
                    'pr_id': pr.id
                }))

        self.write({'bump_pr_ids': commands})

    @api.onchange('bump_pr_ids')
    def _onchange_bump_prs(self):
        labels = {p.pr_id.label for p in self.bump_pr_ids if p.pr_id}
        self.bump_label = len(labels) == 1 and labels.pop()

    @api.depends('release_pr_ids.pr_id.label', 'required_pr_ids.state')
    def _compute_errors(self):
        errors = []

        release_repos = Counter(self.mapped('release_pr_ids.repository_id'))
        release_repos.subtract(self.project_id.repo_ids)
        excess = {k.name for k, v in release_repos.items() if v > 0}
        if excess:
            errors.append("* Every repository must have one release PR, found multiple for %s" % ', '.join(excess))

        without = self.release_pr_ids.filtered(lambda p: not p.pr_id)
        if without:
            errors.append("* Every repository must have a release PR, missing release PRs for %s." % ', '.join(
                without.mapped('repository_id.name')
            ))

        labels = set(self.mapped('release_pr_ids.pr_id.label'))
        if len(labels) != 1:
            errors.append("* All release PRs must have the same label, found %r." % ', '.join(sorted(labels)))
        non_squash = self.mapped('release_pr_ids.pr_id').filtered(lambda p: not p.squash)
        if non_squash:
            errors.append("* Release PRs should have a single commit, found more in %s." % ', '.join(p.display_name for p in non_squash))

        bump_repos = Counter(self.mapped('bump_pr_ids.repository_id'))
        excess = {k.name for k, v in bump_repos.items() if v > 1}
        if excess:
            errors.append("* Every repository may have one bump PR, found multiple for %s" % ', '.join(excess))

        bump_labels = set(self.mapped('bump_pr_ids.pr_id.label'))
        if len(bump_labels) > 1:
            errors.append("* All bump PRs must have the same label, found %r" % ', '.join(sorted(bump_labels)))
        non_squash = self.mapped('bump_pr_ids.pr_id').filtered(lambda p: not p.squash)
        if non_squash:
            errors.append("* Bump PRs should have a single commit, found more in %s." % ', '.join(p.display_name for p in non_squash))

        unready = sum(p.state not in ('closed', 'merged') for p in self.required_pr_ids)
        if unready:
            errors.append(f"* {unready} required PRs not ready.")

        self.errors = '\n'.join(errors) or False

    def action_cancel(self):
        self.project_id.check_access_rights('write')
        self.project_id.check_access_rule('write')
        self.sudo().unlink()

        return {'type': 'ir.actions.act_window_close'}

    def action_open(self):
        return {
            'type': 'ir.actions.act_window',
            'target': 'new',
            'name': f'Freeze project {self.project_id.name}',
            'view_mode': 'form',
            'res_model': self._name,
            'res_id': self.id,
        }

    def action_freeze(self):
        """ Attempts to perform the freeze.
        """
        # if there are still errors, reopen the wizard
        if self.errors:
            return self.action_open()

        conflict_crons = self.env.ref('runbot_merge.merge_cron') | self.env.ref('runbot_merge.staging_cron')
        # we don't want to run concurrently to the crons above, though we
        # don't need to prevent read access to them
        self.env.cr.execute(
            'SELECT * FROM ir_cron WHERE id =ANY(%s) FOR SHARE NOWAIT',
            [conflict_crons.ids]
        )

        project_id = self.project_id
        # need to create the new branch, but at the same time resequence
        # everything so the new branch is the second one, just after the branch
        # it "forks"
        master, rest = project_id.branch_ids[0], project_id.branch_ids[1:]
        seq = itertools.count(start=1) # start reseq at 1
        commands = [
            (1, master.id, {'sequence': next(seq)}),
            (0, 0, {
                'name': self.branch_name,
                'sequence': next(seq),
            })
        ]
        commands.extend((1, b.id, {'sequence': s}) for s, b in zip(seq, rest))
        project_id.branch_ids = commands
        master_name = master.name

        gh_sessions = {r: r.github() for r in self.project_id.repo_ids}

        # prep new branch (via tmp refs) on every repo
        rel_heads = {}
        # store for master heads as odds are high the bump pr(s) will be on the
        # same repo as one of the release PRs
        prevs = {}
        for rel in self.release_pr_ids:
            repo_id = rel.repository_id
            gh = gh_sessions[repo_id]
            try:
                prev = prevs[repo_id] = gh.head(master_name)
            except Exception:
                raise UserError(f"Unable to resolve branch {master_name} of repository {repo_id.name} to a commit.")

            # create the tmp branch to merge the PR into
            tmp_branch = f'tmp.{self.branch_name}'
            try:
                gh.set_ref(tmp_branch, prev)
            except Exception as err:
                raise UserError(f"Unable to create branch {self.branch_name} of repository {repo_id.name}: {err}.")

            rel_heads[repo_id], _ = gh.rebase(rel.pr_id.number, tmp_branch)
            time.sleep(1)

        # prep bump
        bump_heads = {}
        for bump in self.bump_pr_ids:
            repo_id = bump.repository_id
            gh = gh_sessions[repo_id]

            try:
                prev = prevs[repo_id] = prevs.get(repo_id) or gh.head(master_name)
            except Exception:
                raise UserError(f"Unable to resolve branch {master_name} of repository {repo_id.name} to a commit.")

            # create the tmp branch to merge the PR into
            tmp_branch = f'tmp.{master_name}'
            try:
                gh.set_ref(tmp_branch, prev)
            except Exception as err:
                raise UserError(f"Unable to create branch {master_name} of repository {repo_id.name}: {err}.")

            bump_heads[repo_id], _ = gh.rebase(bump.pr_id.number, tmp_branch)
            time.sleep(1)

        deployed = {}
        # at this point we've got a bunch of tmp branches with merged release
        # and bump PRs, it's time to update the corresponding targets
        to_delete = [] # release prs go on new branches which we try to delete on failure
        to_revert = [] # bump prs go on new branch which we try to revert on failure
        failure = None
        for rel in self.release_pr_ids:
            repo_id = rel.repository_id
            # helper API currently has no API to ensure we're just creating a
            # new branch (as cheaply as possible) so do it by hand
            status = None
            with contextlib.suppress(Exception):
                status = gh_sessions[repo_id].create_ref(self.branch_name, rel_heads[repo_id])
                deployed[rel.pr_id.id] = rel_heads[repo_id]
                to_delete.append(repo_id)

            if status != 201:
                failure = ('create', repo_id.name, self.branch_name)
                break
        else: # all release deployments succeeded
            for bump in self.bump_pr_ids:
                repo_id = bump.repository_id
                try:
                    gh_sessions[repo_id].fast_forward(master_name, bump_heads[repo_id])
                    deployed[bump.pr_id.id] = bump_heads[repo_id]
                    to_revert.append(repo_id)
                except FastForwardError:
                    failure = ('fast-forward', repo_id.name, master_name)
                    break

        if failure:
            addendums = []
            # creating the branch failed, try to delete all previous branches
            failures = []
            for prev_id in to_revert:
                revert = gh_sessions[prev_id]('PATCH', f'git/refs/heads/{master_name}', json={
                    'sha': prevs[prev_id],
                    'force': True
                }, check=False)
                if not revert.ok:
                    failures.append(prev_id.name)
            if failures:
                addendums.append(
                    "Subsequently unable to revert branches created in %s." % \
                    ', '.join(failures)
                )
                failures.clear()

            for prev_id in to_delete:
                deletion = gh_sessions[prev_id]('DELETE', f'git/refs/heads/{self.branch_name}', check=False)
                if not deletion.ok:
                    failures.append(prev_id.name)
            if failures:
                addendums.append(
                    "Subsequently unable to delete branches created in %s." % \
                    ", ".join(failures)
                )
                failures.clear()

            if addendums:
                addendum = '\n\n' + '\n'.join(addendums)
            else:
                addendum = ''

            reason, repo, branch = failure
            raise UserError(
                f"Unable to {reason} branch {repo}:{branch}.{addendum}"
            )

        all_prs = self.release_pr_ids.pr_id | self.bump_pr_ids.pr_id
        all_prs.state = 'merged'
        self.env['runbot_merge.pull_requests.feedback'].create([{
            'repository': pr.repository.id,
            'pull_request': pr.number,
            'close': True,
            'message': json.dumps({
                'sha': deployed[pr.id],
                'base': self.branch_name if pr in self.release_pr_ids.pr_id else None
            })
        } for pr in all_prs])

        # delete wizard
        self.sudo().unlink()
        # managed to create all the things, show reminder text (or close)
        if project_id.freeze_reminder:
            return {
                'type': 'ir.actions.act_window',
                'target': 'new',
                'name': f'Freeze reminder {project_id.name}',
                'view_mode': 'form',
                'res_model': project_id._name,
                'res_id': project_id.id,
                'view_id': self.env.ref('runbot_merge.project_freeze_reminder').id
            }

        return {'type': 'ir.actions.act_window_close'}

class ReleasePullRequest(models.Model):
    _name = 'runbot_merge.project.freeze.prs'
    _description = "links to pull requests used to \"cap\" freezes"

    wizard_id = fields.Many2one('runbot_merge.project.freeze', required=True, index=True, ondelete='cascade')
    repository_id = fields.Many2one('runbot_merge.repository', required=True)
    pr_id = fields.Many2one(
        'runbot_merge.pull_requests',
        domain='[("repository", "=", repository_id), ("state", "not in", ("closed", "merged"))]',
        string="Release Pull Request",
    )
    label = fields.Char(related='pr_id.label')

    def write(self, vals):
        # only the pr should be writeable after initial creation
        assert 'wizard_id' not in vals
        assert 'repository_id' not in vals
        # and if the PR gets set, it should match the requested repository
        if 'pr_id' in vals:
            assert self.env['runbot_merge.pull_requests'].browse(vals['pr_id'])\
                       .repository == self.repository_id

        return super().write(vals)

class BumpPullRequest(models.Model):
    _name = 'runbot_merge.project.freeze.bumps'
    _description = "links to pull requests used to \"bump\" the development branches"

    wizard_id = fields.Many2one('runbot_merge.project.freeze', required=True, index=True, ondelete='cascade')
    # FIXME: repo = wizard.repo?
    repository_id = fields.Many2one('runbot_merge.repository', required=True)
    pr_id = fields.Many2one(
        'runbot_merge.pull_requests',
        domain='[("repository", "=", repository_id), ("state", "not in", ("closed", "merged"))]',
        string="Bump Pull Request",
    )
    label = fields.Char(related='pr_id.label')

    @api.onchange('repository_id')
    def _onchange_repository(self):
        self.pr_id = False

    def write(self, vals):
        # only the pr should be writeable after initial creation
        assert 'wizard_id' not in vals
        # and if the PR gets set, it should match the requested repository
        if vals.get('pr_id'):
            assert self.env['runbot_merge.pull_requests'].browse(vals['pr_id'])\
                       .repository == self.repository_id

        return super().write(vals)

class RepositoryFreeze(models.Model):
    _inherit = 'runbot_merge.repository'
    freeze = fields.Boolean(required=True, default=True,
                            help="Freeze this repository by default")

@enum.unique
class Colors(enum.IntEnum):
    No = 0
    Red = 1
    Orange = 2
    Yellow = 3
    LightBlue = 4
    DarkPurple = 5
    Salmon = 6
    MediumBlue = 7
    DarkBlue = 8
    Fuchsia = 9
    Green = 10
    Purple = 11

STATE_COLORMAP = {
    'opened': Colors.No,
    'closed': Colors.Orange,
    'validated': Colors.No,
    'approved': Colors.No,
    'ready': Colors.LightBlue,
    'merged': Colors.Green,
    'error': Colors.Red,
}
class PullRequest(models.Model):
    _inherit = 'runbot_merge.pull_requests'

    state_color = fields.Integer(compute='_compute_state_color')

    @api.depends('state')
    def _compute_state_color(self):
        for p in self:
            p.state_color = STATE_COLORMAP[p.state]
