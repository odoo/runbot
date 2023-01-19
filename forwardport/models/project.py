# -*- coding: utf-8 -*-
"""
Technically could be independent from mergebot but would require a lot of
duplicate work e.g. keeping track of statuses (including on commits which
might not be in PRs yet), handling co-dependent PRs, ...

However extending the mergebot also leads to messiness: fpbot should have
its own user / feedback / API keys, mergebot and fpbot both have branch
ordering but for mergebot it's completely cosmetics, being slaved to mergebot
means PR creation is trickier (as mergebot assumes opened event will always
lead to PR creation but fpbot wants to attach meaning to the PR when setting
it up), ...
"""
import ast
import base64
import collections
import contextlib
import datetime
import itertools
import json
import logging
import operator
import os
import pathlib
import re
import subprocess
import tempfile
import typing

import dateutil.relativedelta
import requests

import resource
from odoo import _, models, fields, api
from odoo.osv import expression
from odoo.exceptions import UserError
from odoo.tools.misc import topological_sort, groupby
from odoo.tools.sql import reverse_order
from odoo.tools.appdirs import user_cache_dir
from odoo.addons.runbot_merge import utils
from odoo.addons.runbot_merge.models.pull_requests import RPLUS

footer = '\nMore info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port\n'

DEFAULT_DELTA = dateutil.relativedelta.relativedelta(days=3)

_logger = logging.getLogger('odoo.addons.forwardport')

class Project(models.Model):
    _inherit = 'runbot_merge.project'

    fp_github_token = fields.Char()
    fp_github_name = fields.Char(store=True, compute="_compute_git_identity")
    fp_github_email = fields.Char(store=True, compute="_compute_git_identity")

    def _find_commands(self, comment):
        if self.env.context.get('without_forward_port'):
            return super()._find_commands(comment)

        return re.findall(
            '^\s*[@|#]?{}:? (.*)$'.format(self.fp_github_name),
            comment, re.MULTILINE | re.IGNORECASE
        ) + super()._find_commands(comment)

    # technically the email could change at any moment...
    @api.depends('fp_github_token')
    def _compute_git_identity(self):
        s = requests.Session()
        for project in self:
            if not project.fp_github_token:
                continue
            r0 = s.get('https://api.github.com/user', headers={
                'Authorization': 'token %s' % project.fp_github_token
            })
            if 'user:email' not in set(re.split(r',\s*', r0.headers['x-oauth-scopes'])):
                raise UserError(_("The forward-port github token needs the user:email scope to fetch the bot's identity."))
            r1 = s.get('https://api.github.com/user/emails', headers={
                'Authorization': 'token %s' % project.fp_github_token
            })
            if not (r0.ok and r1.ok):
                _logger.error("Failed to fetch bot information for project %s: %s", project.name, (r0.text or r0.content) if not r0.ok else (r1.text or r1.content))
                continue
            project.fp_github_name = r0.json()['login']
            project.fp_github_email = next((
                entry['email']
                for entry in r1.json()
                if entry['primary']
            ), None)
            if not project.fp_github_email:
                raise UserError(_("The forward-port bot needs a primary email set up."))

    def write(self, vals):
        # check on branches both active and inactive so disabling branches doesn't
        # make it look like the sequence changed.
        self_ = self.with_context(active_test=False)
        previously_active_branches = {project: project.branch_ids.filtered('active') for project in self_}
        branches_before = {project: project._forward_port_ordered() for project in self_}

        r = super().write(vals)
        self_._followup_prs(previously_active_branches)
        self_._insert_intermediate_prs(branches_before)
        return r

    def _followup_prs(self, previously_active_branches):
        """If a branch has been disabled and had PRs without a followup (e.g.
        because no CI or CI failed), create followup, as if the branch had been
        originally disabled (and thus skipped over)
        """
        PRs = self.env['runbot_merge.pull_requests']
        for p in self:
            actives = previously_active_branches[p]
            for deactivated in p.branch_ids.filtered(lambda b: not b.active) & actives:
                # if a PR targets a deactivated branch, and that's not its limit,
                # and it doesn't have a child (e.g. CI failed), enqueue a forward
                # port as if the now deactivated branch had been skipped over (which
                # is the normal fw behaviour)
                extant = PRs.search([
                    ('target', '=', deactivated.id),
                    ('source_id.limit_id', '!=', deactivated.id),
                    ('state', 'not in', ('closed', 'merged')),
                ])
                for p in extant.with_context(force_fw=True):
                    next_target = p.source_id._find_next_target(p)
                    # should not happen since we already filtered out limits
                    if not next_target:
                        continue

                    # check if it has a descendant in the next branch, if so skip
                    if PRs.search_count([
                        ('source_id', '=', p.source_id.id),
                        ('target', '=', next_target.id)
                    ]):
                        continue

                    # otherwise enqueue a followup
                    p._schedule_fp_followup()

    def _insert_intermediate_prs(self, branches_before):
        """If new branches have been added to the sequence inbetween existing
        branches (mostly a freeze inserted before the main branch), fill in
        forward-ports for existing sequences
        """
        Branches = self.env['runbot_merge.branch']
        for p in self:
            # check if the branches sequence has been modified
            bbefore = branches_before[p]
            bafter = p._forward_port_ordered()
            if bafter.ids == bbefore.ids:
                continue

            logger = _logger.getChild('project').getChild(p.name)
            logger.debug("branches updated %s -> %s", bbefore, bafter)
            # if it's just that a branch was inserted at the end forwardport
            # should keep on keeping normally
            if bafter.ids[:-1] == bbefore.ids:
                continue

            if bafter <= bbefore:
                raise UserError("Branches can not be reordered or removed after saving.")

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
                    'batch_id': self.env['runbot_merge.batch'].create({
                        'target': before[-1].id,
                        'prs': [(4, c.id, 0) for c in cs],
                        'active': False,
                    }).id,
                    'source': 'insert',
                })

    def _forward_port_ordered(self, domain=()):
        Branches = self.env['runbot_merge.branch']
        return Branches.search(expression.AND([
            [('project_id', '=', self.id)],
            domain or [],
        ]), order=reverse_order(Branches._order))

class Repository(models.Model):
    _inherit = 'runbot_merge.repository'
    fp_remote_target = fields.Char(help="where FP branches get pushed")

class Branch(models.Model):
    _inherit = 'runbot_merge.branch'

    fp_target = fields.Boolean(default=True)
    fp_enabled = fields.Boolean(compute='_compute_fp_enabled')

    @api.depends('active', 'fp_target')
    def _compute_fp_enabled(self):
        for b in self:
            b.fp_enabled = b.active and b.fp_target

class PullRequests(models.Model):
    _inherit = 'runbot_merge.pull_requests'

    limit_id = fields.Many2one('runbot_merge.branch', help="Up to which branch should this PR be forward-ported")

    parent_id = fields.Many2one(
        'runbot_merge.pull_requests', index=True,
        help="a PR with a parent is an automatic forward port"
    )
    source_id = fields.Many2one('runbot_merge.pull_requests', index=True, help="the original source of this FP even if parents were detached along the way")
    forwardport_ids = fields.One2many('runbot_merge.pull_requests', 'source_id')
    reminder_backoff_factor = fields.Integer(default=-4, group_operator=None)
    merge_date = fields.Datetime()

    detach_reason = fields.Char()

    fw_policy = fields.Selection([
        ('ci', "Normal"),
        ('skipci', "Skip CI"),
        # ('skipmerge', "Skip merge"),
    ], required=True, default="ci")

    _sql_constraints = [(
        'fw_constraint',
        'check(source_id is null or num_nonnulls(parent_id, detach_reason) = 1)',
        "fw PRs must either be attached or have a reason for being detached",
    )]

    refname = fields.Char(compute='_compute_refname')
    @api.depends('label')
    def _compute_refname(self):
        for pr in self:
            pr.refname = pr.label.split(':', 1)[-1]

    @api.model_create_single
    def create(self, vals):
        # PR opened event always creates a new PR, override so we can precreate PRs
        existing = self.search([
            ('repository', '=', vals['repository']),
            ('number', '=', vals['number']),
        ])
        if existing:
            return existing

        if 'limit_id' not in vals:
            branch = self.env['runbot_merge.branch'].browse(vals['target'])
            repo = self.env['runbot_merge.repository'].browse(vals['repository'])
            vals['limit_id'] = branch.project_id._forward_port_ordered(
                ast.literal_eval(repo.branch_filter or '[]')
            )[-1].id
        if vals.get('parent_id') and 'source_id' not in vals:
            vals['source_id'] = self.browse(vals['parent_id'])._get_root().id
        if vals.get('state') == 'merged':
            vals['merge_date'] = fields.Datetime.now()
        return super().create(vals)

    def write(self, vals):
        # if the PR's head is updated, detach (should split off the FP lines as this is not the original code)
        # TODO: better way to do this? Especially because we don't want to
        #       recursively create updates
        # also a bit odd to only handle updating 1 head at a time, but then
        # again 2 PRs with same head is weird so...
        newhead = vals.get('head')
        with_parents = self.filtered('parent_id')
        closed_fp = self.filtered(lambda p: p.state == 'closed' and p.source_id)
        if newhead and not self.env.context.get('ignore_head_update') and newhead != self.head:
            vals.setdefault('parent_id', False)
            if with_parents and vals['parent_id'] is False:
                vals['detach_reason'] = f"Head updated from {self.head} to {newhead}"
            # if any children, this is an FP PR being updated, enqueue
            # updating children
            if self.search_count([('parent_id', '=', self.id)]):
                self.env['forwardport.updates'].create({
                    'original_root': self._get_root().id,
                    'new_root': self.id
                })

        if vals.get('parent_id') and 'source_id' not in vals:
            vals['source_id'] = self.browse(vals['parent_id'])._get_root().id
        if vals.get('state') == 'merged':
            vals['merge_date'] = fields.Datetime.now()
        r = super().write(vals)
        if self.env.context.get('forwardport_detach_warn', True):
            for p in with_parents:
                if not p.parent_id:
                    self.env['runbot_merge.pull_requests.feedback'].create({
                        'repository': p.repository.id,
                        'pull_request': p.number,
                        'message': "%sthis PR was modified / updated and has become a normal PR. "
                                   "It should be merged the normal way (via @%s)" % (
                            p.source_id.ping(),
                            p.repository.project_id.github_prefix,
                        ),
                        'token_field': 'fp_github_token',
                    })
        for p in closed_fp.filtered(lambda p: p.state != 'closed'):
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': p.repository.id,
                'pull_request': p.number,
                'message': "%sthis PR was closed then reopened. "
                           "It should be merged the normal way (via @%s)" % (
                    p.source_id.ping(),
                    p.repository.project_id.github_prefix,
                ),
                'token_field': 'fp_github_token',
            })
        if vals.get('state') == 'merged':
            for p in self:
                self.env['forwardport.branch_remover'].create({
                    'pr_id': p.id,
                })
        # if we change the policy to skip CI, schedule followups on existing FPs
        if vals.get('fw_policy') == 'skipci' and self.state == 'merged':
            self.env['runbot_merge.pull_requests'].search([
                ('source_id', '=', self.id),
                ('state', 'not in', ('closed', 'merged')),
            ])._schedule_fp_followup()
        return r

    def _try_closing(self, by):
        r = super()._try_closing(by)
        if r:
            self.with_context(forwardport_detach_warn=False).write({
                'parent_id': False,
                'detach_reason': f"Closed by {by}",
            })
            self.search([('parent_id', '=', self.id)]).write({
                'parent_id': False,
                'detach_reason': f"{by} closed parent PR {self.display_name}",
            })
        return r

    def _parse_commands(self, author, comment, login):
        super(PullRequests, self.with_context(without_forward_port=True))._parse_commands(author, comment, login)

        tokens = [
            token
            for line in re.findall('^\s*[@|#]?{}:? (.*)$'.format(self.repository.project_id.fp_github_name), comment['body'] or '', re.MULTILINE | re.IGNORECASE)
            for token in line.split()
        ]
        if not tokens:
            _logger.info("found no commands in comment of %s (%s) (%s)", author.github_login, author.display_name,
                 utils.shorten(comment['body'] or '', 50)
            )
            return

        # TODO: don't use a mutable tokens iterator
        tokens = iter(tokens)
        while True:
            token = next(tokens, None)
            if token is None:
                break

            ping = False
            close = False
            msg = None
            if token in ('ci', 'skipci'):
                pr = (self.source_id or self)
                if pr._pr_acl(author).is_reviewer:
                    pr.fw_policy = token
                    msg = "Not waiting for CI to create followup forward-ports." if token == 'skipci' else "Waiting for CI to create followup forward-ports."
                else:
                    ping = True
                    msg = "you can't configure ci."

            if token == 'ignore': # replace 'ignore' by 'up to <pr_branch>'
                token = 'up'
                tokens = itertools.chain(['to', self.target.name], tokens)

            if token in ('r+', 'review+'):
                if not self.source_id:
                    ping = True
                    msg = "I can only do this on forward-port PRs and this is not one, see {}.".format(
                        self.repository.project_id.github_prefix
                    )
                elif not self.parent_id:
                    ping = True
                    msg = "I can only do this on unmodified forward-port PRs, ask {}.".format(
                        self.repository.project_id.github_prefix
                    )
                else:
                    merge_bot = self.repository.project_id.github_prefix
                    # don't update the root ever
                    for pr in (p for p in self._iter_ancestors() if p.parent_id if p.state in RPLUS):
                        # only the author is delegated explicitely on the
                        pr._parse_commands(author, {**comment, 'body': merge_bot + ' r+'}, login)
            elif token == 'close':
                if self.source_id._pr_acl(author).is_reviewer:
                    close = True
                else:
                    ping = True
                    msg = "you can't close PRs."

            elif token == 'up' and next(tokens, None) == 'to':
                limit = next(tokens, None)
                ping = True
                if not self._pr_acl(author).is_author:
                    msg = "you can't set a forward-port limit.".format(login)
                elif not limit:
                    msg = "please provide a branch to forward-port to."
                else:
                    limit_id = self.env['runbot_merge.branch'].with_context(active_test=False).search([
                        ('project_id', '=', self.repository.project_id.id),
                        ('name', '=', limit),
                    ])
                    if self.source_id:
                        msg = "forward-port limit can only be set on " \
                              f"an origin PR ({self.source_id.display_name} " \
                              "here) before it's merged and forward-ported."
                    elif self.state in ['merged', 'closed']:
                        msg = "forward-port limit can only be set before the PR is merged."
                    elif not limit_id:
                        msg = "there is no branch %r, it can't be used as a forward port target." % limit
                    elif limit_id == self.target:
                        ping = False
                        msg = "Forward-port disabled."
                        self.limit_id = limit_id
                    elif not limit_id.fp_enabled:
                        msg = "branch %r is disabled, it can't be used as a forward port target." % limit_id.name
                    else:
                        ping = False
                        msg = "Forward-porting to %r." % limit_id.name
                        self.limit_id = limit_id

            if msg or close:
                if msg:
                    _logger.info("%s [%s]: %s", self.display_name, login, msg)
                else:
                    _logger.info("%s [%s]: closing", self.display_name, login)
                self.env['runbot_merge.pull_requests.feedback'].create({
                    'repository': self.repository.id,
                    'pull_request': self.number,
                    'message': f'@{author.github_login} {msg}' if msg and ping else msg,
                    'close': close,
                    'token_field': 'fp_github_token',
                })

    def _notify_ci_failed(self, ci):
        # only care about FP PRs which are not staged / merged yet
        # NB: probably ignore approved PRs as normal message will handle them?
        if not (self.state == 'opened' and self.parent_id):
            return

        self.env['runbot_merge.pull_requests.feedback'].create({
            'repository': self.repository.id,
            'pull_request': self.number,
            'token_field': 'fp_github_token',
            'message': '%s%s failed on this forward-port PR' % (
                self.source_id.ping(),
                ci,
            )
        })

    def _validate(self, statuses):
        failed = super()._validate(statuses)
        self._schedule_fp_followup()
        return failed

    def _schedule_fp_followup(self):
        _logger = logging.getLogger(__name__).getChild('forwardport.next')
        # if the PR has a parent and is CI-validated, enqueue the next PR
        for pr in self:
            _logger.info('Checking if forward-port %s (%s)', pr.display_name, pr)
            if not pr.parent_id:
                _logger.info('-> no parent %s (%s)', pr.display_name, pr.parent_id)
                continue
            if not self.env.context.get('force_fw') and self.source_id.fw_policy != 'skipci' and pr.state not in ['validated', 'ready']:
                _logger.info('-> wrong state %s (%s)', pr.display_name, pr.state)
                continue

            # check if we've already forward-ported this branch:
            # it has a batch without a staging
            batch = self.env['runbot_merge.batch'].with_context(active_test=False).search([
                ('staging_id', '=', False),
                ('prs', 'in', pr.id),
            ], limit=1)
            # if the batch is inactive, the forward-port has been done *or*
            # the PR's own forward port is in error, so bail
            if not batch.active:
                _logger.info('-> forward port done or in error (%s.active=%s)', batch, batch.active)
                continue

            # otherwise check if we already have a pending forward port
            _logger.info("%s %s %s", pr.display_name, batch, ', '.join(batch.mapped('prs.display_name')))
            if self.env['forwardport.batches'].search_count([('batch_id', '=', batch.id)]):
                _logger.warning('-> already recorded')
                continue

            # check if batch-mate are all valid
            mates = batch.prs
            # wait until all of them are validated or ready
            if not self.env.context.get('force_fw') and any(pr.source_id.fw_policy != 'skipci' and pr.state not in ('validated', 'ready') for pr in mates):
                _logger.info("-> not ready (%s)", [(pr.display_name, pr.state) for pr in mates])
                continue

            # check that there's no weird-ass state
            if not all(pr.parent_id for pr in mates):
                _logger.warning("Found a batch (%s) with only some PRs having parents, ignoring", mates)
                continue
            if self.search_count([('parent_id', 'in', mates.ids)]):
                _logger.warning("Found a batch (%s) with only some of the PRs having children", mates)
                continue

            _logger.info('-> ok')
            self.env['forwardport.batches'].create({
                'batch_id': batch.id,
                'source': 'fp',
            })

    def _find_next_target(self, reference):
        """ Finds the branch between target and limit_id which follows
        reference
        """
        if reference.target == self.limit_id:
            return
        # NOTE: assumes even disabled branches are properly sequenced, would
        #       probably be a good idea to have the FP view show all branches
        branches = list(self.target.project_id
            .with_context(active_test=False)
            ._forward_port_ordered(ast.literal_eval(self.repository.branch_filter or '[]')))

        # get all branches between max(root.target, ref.target) (excluded) and limit (included)
        from_ = max(branches.index(self.target), branches.index(reference.target))
        to_ = branches.index(self.limit_id)

        # return the first active branch in the set
        return next((
            branch
            for branch in branches[from_+1:to_+1]
            if branch.fp_enabled
        ), None)

    def _commits_lazy(self):
        s = requests.Session()
        s.headers['Authorization'] = 'token %s' % self.repository.project_id.fp_github_token
        for page in itertools.count(1):
            r = s.get('https://api.github.com/repos/{}/pulls/{}/commits'.format(
                self.repository.name,
                self.number
            ), params={'page': page})
            r.raise_for_status()
            yield from r.json()
            if not r.links.get('next'):
                return

    def commits(self):
        """ Returns a PR's commits oldest first (that's what GH does &
        is what we want)
        """
        commits = list(self._commits_lazy())
        # map shas to the position the commit *should* have
        idx =  {
            c: i
            for i, c in enumerate(topological_sort({
                c['sha']: [p['sha'] for p in c['parents']]
                for c in commits
            }))
        }
        return sorted(commits, key=lambda c: idx[c['sha']])

    def _iter_descendants(self):
        pr = self
        while True:
            pr = self.search([('parent_id', '=', pr.id)])
            if pr:
                yield pr
            else:
                break

    @api.depends('parent_id.statuses')
    def _compute_statuses(self):
        super()._compute_statuses()

    def _get_overrides(self):
        # NB: assumes _get_overrides always returns an "owned" dict which we can modify
        p = self.parent_id._get_overrides() if self.parent_id else {}
        p.update(super()._get_overrides())
        return p

    def _iter_ancestors(self):
        while self:
            yield self
            self = self.parent_id

    def _get_root(self):
        root = self
        while root.parent_id:
            root = root.parent_id
        return root

    def _port_forward(self):
        if not self:
            return

        all_sources = [(p.source_id or p) for p in self]
        all_targets = [s._find_next_target(p) for s, p in zip(all_sources, self)]

        ref = self[0]
        base = all_sources[0]
        target = all_targets[0]
        if target is None:
            _logger.info(
                "Will not forward-port %s: no next target",
                ref.display_name,
            )
            return  # QUESTION: do the prs need to be updated?

        # check if the PRs have already been forward-ported: is there a PR
        # with the same source targeting the next branch in the series
        for source in all_sources:
            if self.search_count([('source_id', '=', source.id), ('target', '=', target.id)]):
                _logger.info("Will not forward-port %s: already ported", ref.display_name)
                return

        # check if all PRs in the batch have the same "next target" , bail if
        # that's not the case as it doesn't make sense for forward one PR from
        # a to b and a linked pr from a to c
        different_target = next((t for t in all_targets if t != target), None)
        if different_target:
            different_pr = next(p for p, t in zip(self, all_targets) if t == different_target)
            for pr, t in zip(self, all_targets):
                linked, other = different_pr, different_target
                if t != target:
                    linked, other = ref, target
                self.env['runbot_merge.pull_requests.feedback'].create({
                    'repository': pr.repository.id,
                    'pull_request': pr.number,
                    'token_field': 'fp_github_token',
                    'message': "%sthis pull request can not be forward ported: "
                               "next branch is %r but linked pull request %s "
                               "has a next branch %r." % (
                        pr.ping(), t.name, linked.display_name, other.name
                    )
                })
            _logger.warning(
                "Cancelling forward-port of %s: found different next branches (%s)",
                self, all_targets
            )
            return

        proj = self.mapped('target.project_id')
        if not proj.fp_github_token:
            _logger.warning(
                "Can not forward-port %s: no token on project %s",
                ref.display_name,
                proj.name
            )
            return

        notarget = [p.repository.name for p in self if not p.repository.fp_remote_target]
        if notarget:
            _logger.error(
                "Can not forward-port %s: repos %s don't have a remote configured",
                self, ', '.join(notarget)
            )
            return

        # take only the branch bit
        new_branch = '%s-%s-%s-fw' % (
            target.name,
            base.refname,
            # avoid collisions between fp branches (labels can be reused
            # or conflict especially as we're chopping off the owner)
            base64.urlsafe_b64encode(os.urandom(3)).decode()
        )
        # TODO: send outputs to logging?
        conflicts = {}
        with contextlib.ExitStack() as s:
            for pr in self:
                conflicts[pr], working_copy = pr._create_fp_branch(
                    target, new_branch, s)

                working_copy.push('target', new_branch)

        gh = requests.Session()
        gh.headers['Authorization'] = 'token %s' % proj.fp_github_token
        has_conflicts = any(conflicts.values())
        # problemo: this should forward port a batch at a time, if porting
        # one of the PRs in the batch fails is huge problem, though this loop
        # only concerns itself with the creation of the followup objects so...
        new_batch = self.browse(())
        for pr in self:
            owner, _ = pr.repository.fp_remote_target.split('/', 1)
            source = pr.source_id or pr
            root = pr._get_root()

            message = source.message + '\n\n' + '\n'.join(
                "Forward-Port-Of: %s" % p.display_name
                for p in root | source
            )

            title, body = re.match(r'(?P<title>[^\n]+)\n*(?P<body>.*)', message, flags=re.DOTALL).groups()
            self.env.cr.execute('LOCK runbot_merge_pull_requests IN SHARE MODE')
            r = gh.post(f'https://api.github.com/repos/{pr.repository.name}/pulls', json={
                'base': target.name,
                'head': f'{owner}:{new_branch}',
                'title': '[FW]' + (' ' if title[0] != '[' else '') + title,
                'body': body
            })
            if not r.ok:
                _logger.warning("Failed to create forward-port PR for %s, deleting branches", pr.display_name)
                # delete all the branches this should automatically close the
                # PRs if we've created any. Using the API here is probably
                # simpler than going through the working copies
                for repo in self.mapped('repository'):
                    d = gh.delete(f'https://api.github.com/repos/{repo.fp_remote_target}/git/refs/heads/{new_branch}')
                    if d.ok:
                        _logger.info("Deleting %s:%s=success", repo.fp_remote_target, new_branch)
                    else:
                        _logger.warning("Deleting %s:%s=%s", repo.fp_remote_target, new_branch, d.text)
                raise RuntimeError("Forwardport failure: %s (%s)" % (pr.display_name, r.text))

            new_pr = self._from_gh(r.json())
            _logger.info("Created forward-port PR %s", new_pr)
            new_batch |= new_pr

            # allows PR author to close or skipci
            source.delegates |= source.author
            new_pr.write({
                'merge_method': pr.merge_method,
                'source_id': source.id,
                # only link to previous PR of sequence if cherrypick passed
                'parent_id': pr.id if not has_conflicts else False,
                'detach_reason': "conflicts: {}".format(
                    f'\n{conflicts[pr]}\n{conflicts[pr]}'.strip()
                ) if has_conflicts else None,
                # Copy author & delegates of source as well as delegates of
                # previous so they can r+ the new forward ports.
                'delegates': [(6, False, (source.delegates | pr.delegates).ids)]
            })
            if has_conflicts and pr.parent_id and pr.state not in ('merged', 'closed'):
                message = source.ping() + """\
the next pull request (%s) is in conflict. You can merge the chain up to here by saying
> @%s r+
%s""" % (new_pr.display_name, pr.repository.project_id.fp_github_name, footer)
                self.env['runbot_merge.pull_requests.feedback'].create({
                    'repository': pr.repository.id,
                    'pull_request': pr.number,
                    'message': message,
                    'token_field': 'fp_github_token',
                })
            # not great but we probably want to avoid the risk of the webhook
            # creating the PR from under us. There's still a "hole" between
            # the POST being executed on gh and the commit but...
            self.env.cr.commit()

        for pr, new_pr in zip(self, new_batch):
            source = pr.source_id or pr
            (h, out, err, hh) = conflicts.get(pr) or (None, None, None, None)

            if h:
                sout = serr = ''
                if out.strip():
                    sout = f"\nstdout:\n```\n{out}\n```\n"
                if err.strip():
                    serr = f"\nstderr:\n```\n{err}\n```\n"

                lines = ''
                if len(hh) > 1:
                    lines = '\n' + ''.join(
                        '* %s%s\n' % (sha, ' <- on this commit' if sha == h else '')
                        for sha in hh
                    )
                message = f"""{source.ping()}cherrypicking of pull request {source.display_name} failed.
{lines}{sout}{serr}
Either perform the forward-port manually (and push to this branch, proceeding as usual) or close this PR (maybe?).

In the former case, you may want to edit this PR message as well.
"""
            elif has_conflicts:
                message = """%s\
while this was properly forward-ported, at least one co-dependent PR (%s) did \
not succeed. You will need to fix it before this can be merged.

Both this PR and the others will need to be approved via `@%s r+` as they are \
all considered "in conflict".
%s""" % (
                    source.ping(),
                    ', '.join(p.display_name for p in (new_batch - new_pr)),
                    proj.github_prefix,
                    footer
                )
            elif base._find_next_target(new_pr) is None:
                ancestors = "".join(
                    "* %s\n" % p.display_name
                    for p in pr._iter_ancestors()
                    if p.parent_id
                )
                message = source.ping() + """\
this PR targets %s and is the last of the forward-port chain%s
%s
To merge the full chain, say
> @%s r+
%s""" % (target.name, ' containing:' if ancestors else '.', ancestors, pr.repository.project_id.fp_github_name, footer)
            else:
                message = """\
This PR targets %s and is part of the forward-port chain. Further PRs will be created up to %s.
%s""" % (target.name, base.limit_id.name, footer)
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': new_pr.repository.id,
                'pull_request': new_pr.number,
                'message': message,
                'token_field': 'fp_github_token',
            })
            labels = ['forwardport']
            if has_conflicts:
                labels.append('conflict')
            self.env['runbot_merge.pull_requests.tagging'].create({
                'repository': new_pr.repository.id,
                'pull_request': new_pr.number,
                'tags_add': labels,
            })

        # batch the PRs so _validate can perform the followup FP properly
        # (with the entire batch). If there are conflict then create a
        # deactivated batch so the interface is coherent but we don't pickup
        # an active batch we're never going to deactivate.
        b = self.env['runbot_merge.batch'].create({
            'target': target.id,
            'prs': [(6, 0, new_batch.ids)],
            'active': not has_conflicts,
        })
        # if we're not waiting for CI, schedule followup immediately
        if any(p.source_id.fw_policy == 'skipci' for p in b.prs):
            b.prs[0]._schedule_fp_followup()
        return b

    @property
    def _source_url(self):
        return 'https://{}:{}@github.com/{}'.format(
            self.repository.project_id.fp_github_name or '',
            self.repository.project_id.fp_github_token,
            self.repository.name,
        )

    def _create_fp_branch(self, target_branch, fp_branch_name, cleanup):
        """ Creates a forward-port for the current PR to ``target_branch`` under
        ``fp_branch_name``.

        :param target_branch: the branch to port forward to
        :param fp_branch_name: the name of the branch to create the FP under
        :param ExitStack cleanup: so the working directories can be cleaned up
        :return: A pair of an optional conflict information and a repository. If
                 present the conflict information is composed of the hash of the
                 conflicting commit, the stderr and stdout of the failed
                 cherrypick and a list of all PR commit hashes
        :rtype: (None | (str, str, str, list[commit]), Repo)
        """
        logger = _logger.getChild(str(self.id))
        root = self._get_root()
        logger.info(
            "Forward-porting %s (%s) to %s",
            self.display_name, root.display_name, target_branch.name
        )
        source = self._get_local_directory()
        r = source.with_config(stdout=subprocess.PIPE, stderr=subprocess.STDOUT).fetch()
        logger.info("Updated cache repo %s:\n%s", source._directory, r.stdout.decode())

        logger.info("Create working copy...")
        working_copy = source.clone(
            cleanup.enter_context(
                tempfile.TemporaryDirectory(
                    prefix='%s-to-%s-' % (
                        root.display_name,
                        target_branch.name
                    ),
                    dir=user_cache_dir('forwardport')
                )),
            branch=target_branch.name
        )

        r = working_copy.with_config(stdout=subprocess.PIPE, stderr=subprocess.STDOUT) \
            .fetch(self._source_url, root.head)
        logger.info(
            "Fetched head of %s into %s:\n%s",
            root.display_name,
            working_copy._directory,
            r.stdout.decode()
        )
        if working_copy.check(False).cat_file(e=root.head).returncode:
            raise ForwardPortError(
                f"During forward port of {self.display_name}, unable to find "
                f"expected head of {root.display_name} ({root.head})"
            )

        project_id = self.repository.project_id
        # add target remote
        working_copy.remote(
            'add', 'target',
            'https://{p.fp_github_name}:{p.fp_github_token}@github.com/{r.fp_remote_target}'.format(
                r=self.repository,
                p=project_id
            )
        )
        logger.info("Create FP branch %s in %s", fp_branch_name, working_copy._directory)
        working_copy.checkout(b=fp_branch_name)

        try:
            root._cherry_pick(working_copy)
            return None, working_copy
        except CherrypickError as e:
            h, out, err, commits = e.args

            # using git diff | git apply -3 to get the entire conflict set
            # turns out to not work correctly: in case files have been moved
            # / removed (which turns out to be a common source of conflicts
            # when forward-porting) it'll just do nothing to the working copy
            # so the "conflict commit" will be empty
            # switch to a squashed-pr branch
            working_copy.check(True).checkout('-bsquashed', root.head)
            # commits returns oldest first, so youngest (head) last
            head_commit = commits[-1]['commit']

            to_tuple = operator.itemgetter('name', 'email')
            to_dict = lambda term, vals: {
                'GIT_%s_NAME' % term: vals[0],
                'GIT_%s_EMAIL' % term: vals[1],
                'GIT_%s_DATE' % term: vals[2],
            }
            authors, committers = set(), set()
            for c in (c['commit'] for c in commits):
                authors.add(to_tuple(c['author']))
                committers.add(to_tuple(c['committer']))
            fp_authorship = (project_id.fp_github_name, '', '')
            author = fp_authorship if len(authors) != 1\
                else authors.pop() + (head_commit['author']['date'],)
            committer = fp_authorship if len(committers) != 1 \
                else committers.pop() + (head_commit['committer']['date'],)
            conf = working_copy.with_config(env={
                **to_dict('AUTHOR', author),
                **to_dict('COMMITTER', committer),
                'GIT_COMMITTER_DATE': '',
            })
            # squash to a single commit
            conf.reset('--soft', commits[0]['parents'][0]['sha'])
            conf.commit(a=True, message="temp")
            squashed = conf.stdout().rev_parse('HEAD').stdout.strip().decode()

            # switch back to the PR branch
            conf.checkout(fp_branch_name)
            # cherry-pick the squashed commit to generate the conflict
            conf.with_params('merge.renamelimit=0', 'merge.conflictstyle=diff3')\
                .with_config(check=False)\
                .cherry_pick(squashed, no_commit=True)
            status = conf.stdout().status(short=True, untracked_files='no').stdout.decode()
            if err.strip():
                err = err.rstrip() + '\n----------\nstatus:\n' + status
            else:
                err = 'status:\n' + status
            # if there was a single commit, reuse its message when committing
            # the conflict
            # TODO: still add conflict information to this?
            if len(commits) == 1:
                msg = root._make_fp_message(commits[0])
                conf.with_config(input=str(msg).encode()) \
                    .commit(all=True, allow_empty=True, file='-')
            else:
                conf.commit(
                    all=True, allow_empty=True,
                    message="""Cherry pick of %s failed

stdout:
%s
stderr:
%s
""" % (h, out, err))
            return (h, out, err, [c['sha'] for c in commits]), working_copy

    def _cherry_pick(self, working_copy):
        """ Cherrypicks ``self`` into the working copy

        :return: ``True`` if the cherrypick was successful, ``False`` otherwise
        """
        # <xxx>.cherrypick.<number>
        logger = _logger.getChild(str(self.id)).getChild('cherrypick')

        # original head so we can reset
        prev = original_head = working_copy.stdout().rev_parse('HEAD').stdout.decode().strip()

        commits = self.commits()
        logger.info("%s: copy %s commits to %s\n%s", self, len(commits), original_head, '\n'.join(
            '- %s (%s)' % (c['sha'], c['commit']['message'].splitlines()[0])
            for c in commits
        ))

        for commit in commits:
            commit_sha = commit['sha']
            # config (global -c) or commit options don't really give access to
            # setting dates
            cm = commit['commit'] # get the "git" commit object rather than the "github" commit resource
            env = {
                'GIT_AUTHOR_NAME': cm['author']['name'],
                'GIT_AUTHOR_EMAIL': cm['author']['email'],
                'GIT_AUTHOR_DATE': cm['author']['date'],
                'GIT_COMMITTER_NAME': cm['committer']['name'],
                'GIT_COMMITTER_EMAIL': cm['committer']['email'],
            }
            configured = working_copy.with_config(env=env)

            conf = working_copy.with_config(
                env={**env, 'GIT_TRACE': 'true'},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False
            )
            # first try with default / low renamelimit
            r = conf.cherry_pick(commit_sha)
            logger.debug("Cherry-picked %s: %s\n%s\n%s", commit_sha, r.returncode, r.stdout.decode(), _clean_rename(r.stderr.decode()))
            if r.returncode:
                # if it failed, retry with high renamelimit
                configured.reset('--hard', prev)
                r = conf.with_params('merge.renamelimit=0').cherry_pick(commit_sha)
                logger.debug("Cherry-picked %s (renamelimit=0): %s\n%s\n%s", commit_sha, r.returncode, r.stdout.decode(), _clean_rename(r.stderr.decode()))

            if r.returncode: # pick failed, reset and bail
                # try to log inflateInit: out of memory errors as warning, they
                # seem to return the status code 128
                logger.log(
                    logging.WARNING if r.returncode == 128 else logging.INFO,
                    "forward-port of %s (%s) failed at %s",
                    self, self.display_name, commit_sha)
                configured.reset('--hard', original_head)
                raise CherrypickError(
                    commit_sha,
                    r.stdout.decode(),
                    _clean_rename(r.stderr.decode()),
                    commits
                )

            msg = self._make_fp_message(commit)

            # replace existing commit message with massaged one
            configured \
                .with_config(input=str(msg).encode())\
                .commit(amend=True, file='-')
            prev = configured.stdout().rev_parse('HEAD').stdout.decode()
            logger.info('%s: success -> %s', commit_sha, prev)

    def _build_merge_message(self, message, related_prs=()):
        msg = super()._build_merge_message(message, related_prs=related_prs)

        # ensures all reviewers in the review path are on the PR in order:
        # original reviewer, then last conflict reviewer, then current PR
        reviewers = (self | self._get_root() | self.source_id)\
            .mapped('reviewed_by.formatted_email')

        sobs = msg.headers.getlist('signed-off-by')
        msg.headers.remove('signed-off-by')
        msg.headers.extend(
            ('signed-off-by', signer)
            for signer in sobs
            if signer not in reviewers
        )
        msg.headers.extend(
            ('signed-off-by', reviewer)
            for reviewer in reversed(reviewers)
        )

        return msg

    def _make_fp_message(self, commit):
        cmap = json.loads(self.commits_map)
        msg = self._parse_commit_message(commit['commit']['message'])
        # write the *merged* commit as "original", not the PR's
        msg.headers['x-original-commit'] = cmap.get(commit['sha'], commit['sha'])
        # don't stringify so caller can still perform alterations
        return msg

    def _get_local_directory(self):
        repos_dir = pathlib.Path(user_cache_dir('forwardport'))
        repos_dir.mkdir(parents=True, exist_ok=True)
        repo_dir = repos_dir / self.repository.name

        if repo_dir.is_dir():
            return git(repo_dir)
        else:
            _logger.info("Cloning out %s to %s", self.repository.name, repo_dir)
            subprocess.run(['git', 'clone', '--bare', self._source_url, str(repo_dir)], check=True)
            # bare repos don't have fetch specs by default, and fetching *into*
            # them is a pain in the ass, configure fetch specs so `git fetch`
            # works properly
            repo = git(repo_dir)
            repo.config('--add', 'remote.origin.fetch', '+refs/heads/*:refs/heads/*')
            # negative refspecs require git 2.29
            repo.config('--add', 'remote.origin.fetch', '^refs/heads/tmp.*')
            repo.config('--add', 'remote.origin.fetch', '^refs/heads/staging.*')
            return repo

    def _outstanding(self, cutoff):
        """ Returns "outstanding" (unmerged and unclosed) forward-ports whose
        source was merged before ``cutoff`` (all of them if not provided).

        :param str cutoff: a datetime (ISO-8601 formatted)
        :returns: an iterator of (source, forward_ports)
        """
        return groupby(self.env['runbot_merge.pull_requests'].search([
            # only FP PRs
            ('source_id', '!=', False),
            # active
            ('state', 'not in', ['merged', 'closed']),
            ('source_id.merge_date', '<', cutoff),
        ], order='source_id, id'), lambda p: p.source_id)

    def _hall_of_shame(self):
        """Provides data for the HOS view

        * outstanding forward ports per reviewer
        * pull requests with outstanding forward ports, oldest-merged first
        """
        cutoff_dt = datetime.datetime.now() - DEFAULT_DELTA
        outstanding = self.env['runbot_merge.pull_requests'].search([
            ('source_id', '!=', False),
            ('state', 'not in', ['merged', 'closed']),
            ('source_id.merge_date', '<', cutoff_dt),
        ], order=None)
        # only keep merged because apparently some PRs are in a weird spot
        # where they're sources but closed?
        sources = outstanding.mapped('source_id').filtered('merge_date').sorted('merge_date')
        outstandings = []
        reviewers = collections.Counter()
        for source in sources:
            outstandings.append(Outstanding(source=source, prs=source.forwardport_ids & outstanding))
            reviewers[source.reviewed_by] += 1
        return HallOfShame(
            reviewers=reviewers.most_common(),
            outstanding=outstandings,
        )

    def _reminder(self):
        cutoff = self.env.context.get('forwardport_updated_before') \
              or fields.Datetime.to_string(datetime.datetime.now() - DEFAULT_DELTA)
        cutoff_dt = fields.Datetime.from_string(cutoff)

        for source, prs in self._outstanding(cutoff):
            backoff = dateutil.relativedelta.relativedelta(days=2**source.reminder_backoff_factor)
            prs = list(prs)
            if source.merge_date > (cutoff_dt - backoff):
                continue
            source.reminder_backoff_factor += 1
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': source.repository.id,
                'pull_request': source.number,
                'message': "%sthis pull request has forward-port PRs awaiting action (not merged or closed):\n%s" % (
                    source.ping(),
                    '\n- '.join(pr.display_name for pr in sorted(prs, key=lambda p: p.number))
                ),
                'token_field': 'fp_github_token',
            })

    def ping(self, author=True, reviewer=True):
        source = self.source_id
        if not source:
            return super().ping(author=author, reviewer=reviewer)

        # use a dict literal to maintain ordering (3.6+)
        pingline = ' '.join(
            f'@{p.github_login}'
            for p in filter(None, {
                author and source.author: None,
                reviewer and source.reviewed_by: None,
                reviewer and self.reviewed_by: None,
            })
        )
        return pingline and (pingline + ' ')

class Stagings(models.Model):
    _inherit = 'runbot_merge.stagings'

    def write(self, vals):
        r = super().write(vals)
        # we've just deactivated a successful staging (so it got ~merged)
        if vals.get('active') is False and self.state == 'success':
            # check al batches to see if they should be forward ported
            for b in self.with_context(active_test=False).batch_ids:
                # if all PRs of a batch have parents they're part of an FP
                # sequence and thus handled separately, otherwise they're
                # considered regular merges
                if not all(p.parent_id for p in b.prs):
                    self.env['forwardport.batches'].create({
                        'batch_id': b.id,
                        'source': 'merge',
                    })
        return r

class Feedback(models.Model):
    _inherit = 'runbot_merge.pull_requests.feedback'

    token_field = fields.Selection(selection_add=[('fp_github_token', 'Forwardport Bot')])

ALWAYS = ('gc.auto=0', 'maintenance.auto=0')

def _bypass_limits():
    resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))

def git(directory): return Repo(directory, check=True)
class Repo:
    def __init__(self, directory, **config):
        self._directory = str(directory)
        config.setdefault('stderr', subprocess.PIPE)
        self._config = config
        self._params = ()
        self._opener = subprocess.run

    def __getattr__(self, name):
        return GitCommand(self, name.replace('_', '-'))

    def _run(self, *args, **kwargs):
        opts = {**self._config, **kwargs}
        args = ('git', '-C', self._directory)\
            + tuple(itertools.chain.from_iterable(('-c', p) for p in self._params + ALWAYS))\
            + args
        try:
            return self._opener(args, preexec_fn=_bypass_limits, **opts)
        except subprocess.CalledProcessError as e:
            stream = e.stderr if e.stderr else e.stdout
            if stream:
                _logger.error("git call error: %s", stream)
            raise

    def stdout(self, flag=True):
        if flag is True:
            return self.with_config(stdout=subprocess.PIPE)
        elif flag is False:
            return self.with_config(stdout=None)
        return self.with_config(stdout=flag)

    def lazy(self):
        r = self.with_config()
        r._config.pop('check', None)
        r._opener = subprocess.Popen
        return r

    def check(self, flag):
        return self.with_config(check=flag)

    def with_config(self, **kw):
        opts = {**self._config, **kw}
        r = Repo(self._directory, **opts)
        r._opener = self._opener
        r._params = self._params
        return r

    def with_params(self, *args):
        r = self.with_config()
        r._params = args
        return r

    def clone(self, to, branch=None):
        self._run(
            'clone',
            *([] if branch is None else ['-b', branch]),
            self._directory, to,
        )
        return Repo(to)

class GitCommand:
    def __init__(self, repo, name):
        self._name = name
        self._repo = repo

    def __call__(self, *args, **kwargs):
        return self._repo._run(self._name, *args, *self._to_options(kwargs))

    def _to_options(self, d):
        for k, v in d.items():
            if len(k) == 1:
                yield '-' + k
            else:
                yield '--' + k.replace('_', '-')
            if v not in (None, True):
                assert v is not False
                yield str(v)

class CherrypickError(Exception):
    ...

class ForwardPortError(Exception):
    pass

def _clean_rename(s):
    """ Filters out the "inexact rename detection" spam of cherry-pick: it's
    useless but there seems to be no good way to silence these messages.
    """
    return '\n'.join(
        l for l in s.splitlines()
        if not l.startswith('Performing inexact rename detection')
    )

class HallOfShame(typing.NamedTuple):
    reviewers: list
    outstanding: list

class Outstanding(typing.NamedTuple):
    source: object
    prs: object
