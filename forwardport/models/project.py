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
from __future__ import annotations

import builtins
import datetime
import itertools
import json
import logging
import operator
import subprocess
import typing

import dateutil.relativedelta
import requests

from odoo import models, fields, api
from odoo.exceptions import UserError
from odoo.fields import Domain
from odoo.tools.misc import topological_sort, groupby
from odoo.addons.base.models.res_partner import Partner
from odoo.addons.runbot_merge import git, utils
from odoo.addons.runbot_merge.models.pull_requests import Branch
from odoo.addons.runbot_merge.models.stagings_create import Message

Conflict = tuple[str, str, str, list[str]]

DEFAULT_DELTA = dateutil.relativedelta.relativedelta(days=3)

_logger = logging.getLogger('odoo.addons.forwardport')

class Project(models.Model):
    _inherit = 'runbot_merge.project'

    id: int
    github_prefix: str

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

        for feedback in self.env['runbot_merge.pull_requests.feedback'].search(Domain.OR(
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
                    'batch_id': cs[0].batch_id.id,
                    'source': 'insert',
                })

class Repository(models.Model):
    _inherit = 'runbot_merge.repository'

    id: int
    project_id: Project
    name: str
    branch_filter: str
    fp_remote_target = fields.Char(help="where FP branches get pushed")

class PullRequests(models.Model):
    _inherit = 'runbot_merge.pull_requests'

    id: int
    display_name: str
    number: int
    repository: Repository
    target: Branch
    reviewed_by: Partner
    head: str
    state: str
    merge_date: datetime.datetime
    parent_id: PullRequests

    reminder_backoff_factor = fields.Integer(default=-4, group_operator=None)

    @api.model_create_multi
    def create(self, vals_list):
        created = []
        to_create = []
        old = self.browse(())
        for vals in vals_list:
            # PR opened event always creates a new PR, override so we can precreate PRs
            existing = self.search([
                ('repository', '=', vals['repository']),
                ('number', '=', vals['number']),
            ])
            created.append(not existing)
            if existing:
                old |= existing
                continue
            to_create.append(vals)
            if vals.get('parent_id') and 'source_id' not in vals:
                vals['source_id'] = self.browse(vals['parent_id']).root_id.id
        if not to_create:
            return old

        new = super().create(to_create)
        for pr in new:
            # added a new PR to an already forward-ported batch: port the PR
            if self.env['runbot_merge.batch'].search_count([
                ('parent_id', '=', pr.batch_id.id),
            ]):
                self.env['forwardport.batches'].create({
                    'batch_id': pr.batch_id.id,
                    'source': 'complete',
                    'pr_id': pr.id,
                })
        if not old:
            return new

        new = iter(new)
        old = iter(old)
        return self.browse(next(new).id if c else next(old).id for c in created)

    def write(self, vals):
        # if the PR's head is updated, detach (should split off the FP lines as this is not the original code)
        # TODO: better way to do this? Especially because we don't want to
        #       recursively create updates
        # also a bit odd to only handle updating 1 head at a time, but then
        # again 2 PRs with same head is weird so...
        newhead = vals.get('head')
        with_parents = {
            p: p.parent_id
            for p in self
            if p.state not in ('merged', 'closed')
            if p.parent_id
        }
        closed_fp = self.filtered(lambda p: p.state == 'closed' and p.source_id)
        if newhead and not self.env.context.get('ignore_head_update') and newhead != self.head:
            vals.setdefault('parent_id', False)
            if with_parents and vals['parent_id'] is False:
                vals['detach_reason'] = f"Head updated from {self.head} to {newhead}"
            # if any children, this is an FP PR being updated, enqueue
            # updating children
            if self.search_count([('parent_id', '=', self.id)]):
                self.env['forwardport.updates'].create({
                    'original_root': self.root_id.id,
                    'new_root': self.id
                })

        if vals.get('parent_id') and 'source_id' not in vals:
            parent = self.browse(vals['parent_id'])
            vals['source_id'] = (parent.source_id or parent).id
        r = super().write(vals)
        if self.env.context.get('forwardport_detach_warn', True):
            for p, parent in with_parents.items():
                if p.parent_id:
                    continue
                self.env.ref('runbot_merge.forwardport.update.detached')._send(
                    repository=p.repository,
                    pull_request=p.number,
                    token_field='fp_github_token',
                    format_args={'pr': p},
                )
                if parent.state not in ('closed', 'merged'):
                    self.env.ref('runbot_merge.forwardport.update.parent')._send(
                        repository=parent.repository,
                        pull_request=parent.number,
                        token_field='fp_github_token',
                        format_args={'pr': parent, 'child': p},
                    )
        return r

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


    def _create_port_branch(
            self,
            source: git.Repo,
            target_branch: Branch,
            *,
            forward: bool,
    ) -> tuple[typing.Optional[Conflict], str]:
        """ Creates a forward-port for the current PR to ``target_branch`` under
        ``fp_branch_name``.

        :param source: the git repository to work with / in
        :param target_branch: the branch to port ``self`` to
        :param forward: whether this is a forward (``True``) or a back
                        (``False``) port
        :returns: a conflict if one happened, and the head of the port branch
                  (either a succcessful port of the entire `self`, or a conflict
                  commit)
        """
        logger = _logger.getChild(str(self.id))
        root = self.root_id
        logger.info(
            "%s %s (%s) to %s",
            "Forward-porting" if forward else "Back-porting",
            self.display_name, root.display_name, target_branch.name
        )
        fetch = source.with_config(stdout=subprocess.PIPE, stderr=subprocess.STDOUT).fetch()
        logger.info("Updated cache repo %s:\n%s", source._directory, fetch.stdout.decode())

        head_fetch = source.with_config(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False) \
            .fetch(git.source_url(self.repository), root.head)
        if head_fetch.returncode:
            raise ForwardPortError(
                f"During forward port of {self.display_name}, unable to find "
                f"expected head of {root.display_name} ({root.head})"
            )

        logger.info(
            "Fetched head of %s (%s):\n%s",
            root.display_name,
            root.head,
            head_fetch.stdout.decode()
        )

        try:
            return None, root._cherry_pick(source, target_branch.name)
        except CherrypickError as e:
            h, out, err, commits = e.args

            # commits returns oldest first, so youngest (head) last
            head_commit = commits[-1]['commit']

            to_tuple = operator.itemgetter('name', 'email')
            authors, committers = set(), set()
            for commit in (c['commit'] for c in commits):
                authors.add(to_tuple(commit['author']))
                committers.add(to_tuple(commit['committer']))
            fp_authorship = (self.repository.project_id.fp_github_name, '', '')
            author = fp_authorship if len(authors) != 1 \
                else authors.pop() + (head_commit['author']['date'],)
            committer = fp_authorship if len(committers) != 1 \
                else committers.pop() + (head_commit['committer']['date'],)
            conf = source.with_params(
                'merge.renamelimit=0',
                'merge.renames=copies',
                'merge.conflictstyle=zdiff3'
            ).with_config(stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            tree = conf.with_config(check=False).merge_tree(
                '--merge-base', commits[0]['parents'][0]['sha'],
                target_branch.name,
                root.head,
            ).stdout.decode().splitlines(keepends=False)[0]
            # if there was a single commit, reuse its message when committing
            # the conflict
            if len(commits) == 1:
                msg = root._make_fp_message(commits[0])
            else:
                out = utils.shorten(out, 8*1024, '[...]')
                err = utils.shorten(err, 8*1024, '[...]')
                msg = f"""Cherry pick of {h} failed

stdout:
{out}
stderr:
{err}
"""

            # if a file is modified by the original PR and added by the forward
            # port / conflict it's a modify/delete conflict: the file was
            # deleted in the target branch, and the update (modify) in the
            # original PR causes it to be added back
            original_modified = set(conf.diff(
                "--diff-filter=M", "--name-only",
                "--merge-base", root.target.name,
                root.head,
            ).stdout.decode().splitlines(keepends=False))
            conflict_added = set(conf.diff(
                "--diff-filter=A", "--name-only",
                target_branch.name,
                tree,
            ).stdout.decode().splitlines(keepends=False))
            if modify_delete := (conflict_added & original_modified):
                # rewrite the tree with conflict markers added to modify/deleted blobs
                tree = conf.modify_delete(tree, modify_delete)

            target_head = source.stdout().rev_parse(f"refs/heads/{target_branch.name}")\
                .stdout.decode().strip()
            commit = conf.commit_tree(
                tree=tree,
                parents=[target_head],
                message=str(msg),
                author=author,
                committer=committer[:2],
            )
            assert commit.returncode == 0,\
                f"commit failed\n\n{commit.stdout.decode()}\n\n{commit.stderr.decode}"
            hh = commit.stdout.strip()

            return (h, out, err, [c['sha'] for c in commits]), hh

    def _cherry_pick(self, repo: git.Repo, branch: Branch) -> str:
        """ Cherrypicks ``self`` into ``branch``

        :return: the HEAD of the forward-port is successful
        :raises CherrypickError: in case of conflict
        """
        # <xxx>.cherrypick.<number>
        logger = _logger.getChild(str(self.id)).getChild('cherrypick')

        # target's head
        head = repo.stdout().rev_parse(f"refs/heads/{branch}").stdout.decode().strip()

        commits = self.commits()
        logger.info(
            "%s: copy %s commits to %s (%s)%s",
            self, len(commits), branch, head, ''.join(
                '\n- %s: %s' % (c['sha'], c['commit']['message'].splitlines()[0])
                for c in commits
            )
        )

        conf = repo.with_params(
            'merge.renamelimit=0',
            'merge.renames=copies',
        ).with_config(
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        for commit in commits:
            commit_sha = commit['sha']
            # merge-tree is a bit stupid and gets confused when the options
            # follow the parameters
            r = conf.merge_tree('--merge-base', commit['parents'][0]['sha'], head, commit_sha)
            new_tree = r.stdout.decode().splitlines(keepends=False)[0]
            if r.returncode:
                # For merge-tree the stdout on conflict is of the form
                #
                # oid of toplevel tree
                # conflicted file info+
                #
                # informational messages+
                #
                # to match cherrypick we only want the informational messages,
                # so strip everything else
                r.stdout = r.stdout.split(b'\n\n')[-1]
            else:
                # By default cherry-pick fails if a non-empty commit becomes
                # empty (--empty=stop), also it fails when cherrypicking already
                # empty commits which I didn't think we prevented but clearly we
                # do...?
                parent_tree = conf.rev_parse(f'{head}^{{tree}}').stdout.decode().strip()
                if parent_tree == new_tree:
                    r.returncode = 1
                    r.stdout = f"You are currently cherry-picking commit {commit_sha}.".encode()
                    r.stderr = b"The previous cherry-pick is now empty, possibly due to conflict resolution."

            logger.debug("Cherry-picked %s: %s\n%s\n%s", commit_sha, r.returncode, r.stdout.decode(), _clean_rename(r.stderr.decode()))
            if r.returncode: # pick failed, bail
                # try to log inflateInit: out of memory errors as warning, they
                # seem to return the status code 128 (nb: may not work anymore
                # with merge-tree, idk)
                logger.log(
                    logging.WARNING if r.returncode == 128 else logging.INFO,
                    "forward-port of %s (%s) failed at %s",
                    self, self.display_name, commit_sha)

                raise CherrypickError(
                    commit_sha,
                    r.stdout.decode(),
                    _clean_rename(r.stderr.decode()),
                    commits
                )
            # get the "git" commit object rather than the "github" commit resource
            cc = conf.commit_tree(
                tree=new_tree,
                parents=[head],
                message=str(self._make_fp_message(commit)),
                author=map_author(commit['commit']['author']),
                committer=map_committer(commit['commit']['committer']),
            )
            if cc.returncode:
                raise CherrypickError(commit_sha, cc.stdout.decode(), cc.stderr.decode(), commits)

            head = cc.stdout.strip()
            logger.info('%s -> %s', commit_sha, head)

        return head

    def _make_fp_message(self, commit):
        cmap = json.loads(self.commits_map)
        msg = Message.from_message(commit['commit']['message'])
        # write the *merged* commit as "original", not the PR's
        msg.headers['x-original-commit'] = cmap.get(commit['sha'], commit['sha'])
        # don't stringify so caller can still perform alterations
        return msg

    def _outstanding(self, cutoff: str) -> typing.ItemsView[PullRequests, list[PullRequests]]:
        """ Returns "outstanding" (unmerged and unclosed) forward-ports whose
        source was merged before ``cutoff`` (all of them if not provided).

        :param cutoff: a datetime (ISO-8601 formatted)
        :returns: an iterator of (source, forward_ports)
        """
        return groupby(self.env['runbot_merge.pull_requests'].search([
            # only FP PRs
            ('source_id', '!=', False),
            # active
            ('state', 'not in', ['merged', 'closed']),
            # not ready
            ('blocked', '!=', False),
            ('source_id.merge_date', '<', cutoff),
        ], order='source_id, id'), lambda p: p.source_id)

    def _reminder(self):
        cutoff = getattr(builtins, 'forwardport_updated_before', None) \
              or fields.Datetime.to_string(datetime.datetime.now() - DEFAULT_DELTA)
        cutoff_dt = fields.Datetime.from_string(cutoff)

        for source, prs in self._outstanding(cutoff):
            backoff = dateutil.relativedelta.relativedelta(days=2**source.reminder_backoff_factor)
            if source.merge_date > (cutoff_dt - backoff):
                continue
            source.reminder_backoff_factor += 1

            # only keep the PRs which don't have an attached descendant
            for pr in set(prs).difference(p.parent_id for p in prs):
                self.env.ref('runbot_merge.forwardport.reminder')._send(
                    repository=pr.repository,
                    pull_request=pr.number,
                    token_field='fp_github_token',
                    format_args={'pr': pr, 'source': source},
                )


map_author = operator.itemgetter('name', 'email', 'date')
map_committer = operator.itemgetter('name', 'email')


class Stagings(models.Model):
    _inherit = 'runbot_merge.stagings'

    def write(self, vals):
        r = super().write(vals)
        # we've just deactivated a successful staging (so it got ~merged)
        if vals.get('active') is False and self.state == 'success':
            # check all batches to see if they should be forward ported
            for b in self.with_context(active_test=False).batch_ids:
                if b.fw_policy == 'no':
                    continue

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
