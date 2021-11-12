# coding: utf-8

import ast
import base64
import collections
import datetime
import io
import itertools
import json
import logging
import os
import pprint
import re
import time

from itertools import takewhile

import requests
import werkzeug
from werkzeug.datastructures import Headers

from odoo import api, fields, models, tools
from odoo.exceptions import ValidationError
from odoo.osv import expression
from odoo.tools import OrderedSet

from .. import github, exceptions, controllers, utils

WAIT_FOR_VISIBILITY = [10, 10, 10, 10]

_logger = logging.getLogger(__name__)


class StatusConfiguration(models.Model):
    _name = 'runbot_merge.repository.status'
    _description = "required statuses on repositories"
    _rec_name = 'context'
    _log_access = False

    context = fields.Char(required=True)
    repo_id = fields.Many2one('runbot_merge.repository', required=True, ondelete='cascade')
    branch_filter = fields.Char(help="branches this status applies to")
    prs = fields.Boolean(string="Applies to pull requests", default=True)
    stagings = fields.Boolean(string="Applies to stagings", default=True)

    def _for_branch(self, branch):
        assert branch._name == 'runbot_merge.branch', \
            f'Expected branch, got {branch}'
        return self.filtered(lambda st: (
            not st.branch_filter
            or branch.filtered_domain(ast.literal_eval(st.branch_filter))
        ))
    def _for_pr(self, pr):
        assert pr._name == 'runbot_merge.pull_requests', \
            f'Expected pull request, got {pr}'
        return self._for_branch(pr.target).filtered('prs')
    def _for_staging(self, staging):
        assert staging._name == 'runbot_merge.stagings', \
            f'Expected staging, got {staging}'
        return self._for_branch(staging.target).filtered('stagings')

class Repository(models.Model):
    _name = _description = 'runbot_merge.repository'
    _order = 'sequence, id'

    sequence = fields.Integer(default=50)
    name = fields.Char(required=True)
    project_id = fields.Many2one('runbot_merge.project', required=True)
    status_ids = fields.One2many('runbot_merge.repository.status', 'repo_id', string="Required Statuses")

    group_id = fields.Many2one('res.groups', default=lambda self: self.env.ref('base.group_user'))

    branch_filter = fields.Char(default='[(1, "=", 1)]', help="Filter branches valid for this repository")
    substitutions = fields.Text(
        "label substitutions",
        help="""sed-style substitution patterns applied to the label on input, one per line.

All substitutions are tentatively applied sequentially to the input.
""")

    @api.model
    def create(self, vals):
        if 'status_ids' in vals:
            return super().create(vals)

        st = vals.pop('required_statuses', 'legal/cla,ci/runbot')
        if st:
            vals['status_ids'] = [(0, 0, {'context': c}) for c in st.split(',')]
        return super().create(vals)

    def write(self, vals):
        st = vals.pop('required_statuses', None)
        if st:
            vals['status_ids'] = [(5, 0, {})] + [(0, 0, {'context': c}) for c in st.split(',')]
        return super().write(vals)

    def github(self, token_field='github_token'):
        return github.GH(self.project_id[token_field], self.name)

    def _auto_init(self):
        res = super(Repository, self)._auto_init()
        tools.create_unique_index(
            self._cr, 'runbot_merge_unique_repo', self._table, ['name'])
        return res

    def _load_pr(self, number):
        gh = self.github()

        # fetch PR object and handle as *opened*
        issue, pr = gh.pr(number)

        feedback = self.env['runbot_merge.pull_requests.feedback'].create
        if not self.project_id._has_branch(pr['base']['ref']):
            _logger.info("Tasked with loading PR %d for un-managed branch %s:%s, ignoring",
                         number, self.name, pr['base']['ref'])
            feedback({
                'repository': self.id,
                'pull_request': number,
                'message': "I'm sorry. Branch `{}` is not within my remit.".format(pr['base']['ref']),
            })
            return

        # if the PR is already loaded, check... if the heads match?
        pr_id = self.env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', pr['base']['repo']['full_name']),
            ('number', '=', number),
        ])
        if pr_id:
            # TODO: edited, maybe (requires crafting a 'changes' object)
            r = controllers.handle_pr(self.env, {
                'action': 'synchronize',
                'pull_request': pr,
                'sender': {'login': self.project_id.github_prefix}
            })
            feedback({
                'repository': pr_id.repository.id,
                'pull_request': number,
                'message': r,
            })
            return

        feedback({
            'repository': self.id,
            'pull_request': number,
            'message': "Sorry, I didn't know about this PR and had to retrieve "
                       "its information, you may have to re-approve it."
        })
        # init the PR to the null commit so we can later synchronise it back
        # back to the "proper" head while resetting reviews
        controllers.handle_pr(self.env, {
            'action': 'opened',
            'pull_request': {
                **pr,
                'head': {**pr['head'], 'sha': '0'*40},
                'state': 'open',
            },
        })
        # fetch & set up actual head
        for st in gh.statuses(pr['head']['sha']):
            controllers.handle_status(self.env, st)
        # fetch and apply comments
        counter = itertools.count()
        items = [ # use counter so `comment` and `review` don't get hit during sort
            (comment['created_at'], next(counter), False, comment)
            for comment in gh.comments(number)
        ] + [
            (review['submitted_at'], next(counter), True, review)
            for review in gh.reviews(number)
        ]
        items.sort()
        for _, _, is_review, item in items:
            if is_review:
                controllers.handle_review(self.env, {
                    'action': 'submitted',
                    'review': item,
                    'pull_request': pr,
                    'repository': {'full_name': self.name},
                })
            else:
                controllers.handle_comment(self.env, {
                    'action': 'created',
                    'issue': issue,
                    'sender': item['user'],
                    'comment': item,
                    'repository': {'full_name': self.name},
                })
        # sync to real head
        controllers.handle_pr(self.env, {
            'action': 'synchronize',
            'pull_request': pr,
            'sender': {'login': self.project_id.github_prefix}
        })
        if pr['state'] == 'closed':
            # don't go through controller because try_closing does weird things
            # for safety / race condition reasons which ends up committing
            # and breaks everything
            self.env['runbot_merge.pull_requests'].search([
                ('repository.name', '=', pr['base']['repo']['full_name']),
                ('number', '=', number),
            ]).state = 'closed'

    def having_branch(self, branch):
        branches = self.env['runbot_merge.branch'].search
        return self.filtered(lambda r: branch in branches(ast.literal_eval(r.branch_filter)))

    def _remap_label(self, label):
        for line in filter(None, (self.substitutions or '').splitlines()):
            sep = line[0]
            _, pattern, repl, flags = line.split(sep)
            label = re.sub(
                pattern, repl, label,
                count=0 if 'g' in flags else 1,
                flags=(re.MULTILINE if 'm' in flags.lower() else 0)
                    | (re.IGNORECASE if 'i' in flags.lower() else 0)
            )
        return label

class Branch(models.Model):
    _name = _description = 'runbot_merge.branch'
    _order = 'sequence, name'

    name = fields.Char(required=True)
    project_id = fields.Many2one('runbot_merge.project', required=True)

    active_staging_id = fields.Many2one(
        'runbot_merge.stagings', compute='_compute_active_staging', store=True,
        help="Currently running staging for the branch."
    )
    staging_ids = fields.One2many('runbot_merge.stagings', 'target')
    split_ids = fields.One2many('runbot_merge.split', 'target')

    prs = fields.One2many('runbot_merge.pull_requests', 'target', domain=[
        ('state', '!=', 'closed'),
        ('state', '!=', 'merged'),
    ])

    active = fields.Boolean(default=True)
    sequence = fields.Integer()

    def _auto_init(self):
        res = super(Branch, self)._auto_init()
        tools.create_unique_index(
            self._cr, 'runbot_merge_unique_branch_per_repo',
            self._table, ['name', 'project_id'])
        return res

    @api.depends('staging_ids.active')
    def _compute_active_staging(self):
        for b in self:
            b.active_staging_id = b.with_context(active_test=True).staging_ids

    def _ready(self):
        self.env.cr.execute("""
        SELECT
          min(pr.priority) as priority,
          array_agg(pr.id) AS match
        FROM runbot_merge_pull_requests pr
        WHERE pr.target = any(%s)
          -- exclude terminal states (so there's no issue when
          -- deleting branches & reusing labels)
          AND pr.state != 'merged'
          AND pr.state != 'closed'
        GROUP BY
            pr.target,
            CASE
                WHEN pr.label SIMILAR TO '%%:patch-[[:digit:]]+'
                    THEN pr.id::text
                ELSE pr.label
            END
        HAVING
            bool_or(pr.state = 'ready') or bool_or(pr.priority = 0)
        ORDER BY min(pr.priority), min(pr.id)
        """, [self.ids])
        browse = self.env['runbot_merge.pull_requests'].browse
        return [(p, browse(ids)) for p, ids in self.env.cr.fetchall()]

    def _stageable(self):
        return [
            (p, prs)
            for p, prs in self._ready()
            if not any(prs.mapped('blocked'))
        ]

    def try_staging(self):
        """ Tries to create a staging if the current branch does not already
        have one. Returns None if the branch already has a staging or there
        is nothing to stage, the newly created staging otherwise.
        """
        logger = _logger.getChild('cron')

        logger.info(
            "Checking %s (%s) for staging: %s, skip? %s",
            self, self.name,
            self.active_staging_id,
            bool(self.active_staging_id)
        )
        if self.active_staging_id:
            return

        rows = self._stageable()
        priority = rows[0][0] if rows else -1
        if priority == 0 or priority == 1:
            # p=0 take precedence over all else
            # p=1 allows merging a fix inside / ahead of a split (e.g. branch
            # is broken or widespread false positive) without having to cancel
            # the existing staging
            batched_prs = [pr_ids for _, pr_ids in takewhile(lambda r: r[0] == priority, rows)]
        elif self.split_ids:
            split_ids = self.split_ids[0]
            logger.info("Found split of PRs %s, re-staging", split_ids.mapped('batch_ids.prs'))
            batched_prs = [batch.prs for batch in split_ids.batch_ids]
            split_ids.unlink()
        else: # p=2
            batched_prs = [pr_ids for _, pr_ids in takewhile(lambda r: r[0] == priority, rows)]

        if not batched_prs:
            return

        Batch = self.env['runbot_merge.batch']
        staged = Batch
        original_heads = {}
        meta = {repo: {} for repo in self.project_id.repo_ids.having_branch(self)}
        for repo, it in meta.items():
            gh = it['gh'] = repo.github()
            it['head'] = original_heads[repo] = gh.head(self.name)
            # create tmp staging branch
            gh.set_ref('tmp.{}'.format(self.name), it['head'])

        batch_limit = self.project_id.batch_limit
        first = True
        for batch in batched_prs:
            if len(staged) >= batch_limit:
                break
            try:
                staged |= Batch.stage(meta, batch)
            except exceptions.MergeError as e:
                pr = e.args[0]
                _logger.exception("Failed to merge %s into staging branch", pr.display_name)
                if first or isinstance(e, exceptions.Unmergeable):
                    if len(e.args) > 1 and e.args[1]:
                        message = e.args[1]
                    else:
                        message = "Unable to stage PR (%s)" % e.__context__
                    pr.state = 'error'
                    self.env['runbot_merge.pull_requests.feedback'].create({
                        'repository': pr.repository.id,
                        'pull_request': pr.number,
                        'message': message,
                    })
            else:
                first = False

        if not staged:
            return

        heads = {}
        for repo, it in meta.items():
            tree = it['gh'].commit(it['head'])['tree']
            # ensures staging branches are unique and always
            # rebuilt
            r = base64.b64encode(os.urandom(12)).decode('ascii')
            trailer = ''
            if heads:
                trailer = '\n'.join(
                    'Runbot-dependency: %s:%s' % (repo, h)
                    for repo, h in heads.items()
                    if not repo.endswith('^')
                )
            dummy_head = {'sha': it['head']}
            if it['head'] == original_heads[repo]:
                # if the repo has not been updated by the staging, create a
                # dummy commit to force rebuild
                dummy_head = it['gh']('post', 'git/commits', json={
                    'message': '''force rebuild

uniquifier: %s
For-Commit-Id: %s
%s''' % (r, it['head'], trailer),
                    'tree': tree['sha'],
                    'parents': [it['head']],
                }).json()

            # $repo is the head to check, $repo^ is the head to merge (they
            # might be the same)
            heads[repo.name + '^'] = it['head']
            heads[repo.name] = dummy_head['sha']
            self.env.cr.execute(
                "INSERT INTO runbot_merge_commit (sha, to_check, statuses) "
                "VALUES (%s, true, '{}') "
                "ON CONFLICT (sha) DO UPDATE SET to_check=true",
                [dummy_head['sha']]
            )

        # create actual staging object
        st = self.env['runbot_merge.stagings'].create({
            'target': self.id,
            'batch_ids': [(4, batch.id, 0) for batch in staged],
            'heads': json.dumps(heads)
        })
        # create staging branch from tmp
        token = self.project_id.github_token
        for r in self.project_id.repo_ids.having_branch(self):
            it = meta[r]
            staging_head = heads[r.name]
            _logger.info(
                "%s: create staging for %s:%s at %s",
                self.project_id.name, r.name, self.name,
                staging_head
            )
            refname = 'staging.{}'.format(self.name)
            it['gh'].set_ref(refname, staging_head)
            # asserts that the new head is visible through the api
            head = it['gh'].head(refname)
            assert head == staging_head,\
                "[api] updated %s:%s to %s but found %s" % (
                    r.name, refname,
                    staging_head, head,
                )

            i = itertools.count()
            @utils.backoff(delays=WAIT_FOR_VISIBILITY, exc=TimeoutError)
            def wait_for_visibility():
                if self._check_visibility(r, refname, staging_head, token):
                    _logger.info(
                        "[repo] updated %s:%s to %s: ok (at %d/%d)",
                        r.name, refname, staging_head,
                        next(i), len(WAIT_FOR_VISIBILITY)
                    )
                    return
                _logger.warning(
                    "[repo] updated %s:%s to %s: failed (at %d/%d)",
                    r.name, refname, staging_head,
                    next(i), len(WAIT_FOR_VISIBILITY)
                )
                raise TimeoutError("Staged head not updated after %d seconds" % sum(WAIT_FOR_VISIBILITY))

        logger.info("Created staging %s (%s) to %s", st, ', '.join(
            '%s[%s]' % (batch, batch.prs)
            for batch in staged
        ), st.target.name)
        return st

    def _check_visibility(self, repo, branch_name, expected_head, token):
        """ Checks the repository actual to see if the new / expected head is
        now visible
        """
        # v1 protocol provides URL for ref discovery: https://github.com/git/git/blob/6e0cc6776106079ed4efa0cc9abace4107657abf/Documentation/technical/http-protocol.txt#L187
        # for more complete client this is also the capabilities discovery and
        # the "entry point" for the service
        url = 'https://github.com/{}.git/info/refs?service=git-upload-pack'.format(repo.name)
        with requests.get(url, stream=True, auth=(token, '')) as resp:
            if not resp.ok:
                return False
            for head, ref in parse_refs_smart(resp.raw.read):
                if ref != ('refs/heads/' + branch_name):
                    continue
                return head == expected_head
            return False

ACL = collections.namedtuple('ACL', 'is_admin is_reviewer is_author')
class PullRequests(models.Model):
    _name = _description = 'runbot_merge.pull_requests'
    _order = 'number desc'
    _rec_name = 'number'

    target = fields.Many2one('runbot_merge.branch', required=True, index=True)
    repository = fields.Many2one('runbot_merge.repository', required=True)
    # NB: check that target & repo have same project & provide project related?

    state = fields.Selection([
        ('opened', 'Opened'),
        ('closed', 'Closed'),
        ('validated', 'Validated'),
        ('approved', 'Approved'),
        ('ready', 'Ready'),
        # staged?
        ('merged', 'Merged'),
        ('error', 'Error'),
    ], default='opened', index=True)

    number = fields.Integer(required=True, index=True)
    author = fields.Many2one('res.partner')
    head = fields.Char(required=True)
    label = fields.Char(
        required=True, index=True,
        help="Label of the source branch (owner:branchname), used for "
             "cross-repository branch-matching"
    )
    message = fields.Text(required=True)
    draft = fields.Boolean(default=False, required=True)
    squash = fields.Boolean(default=False)
    merge_method = fields.Selection([
        ('merge', "merge directly, using the PR as merge commit message"),
        ('rebase-merge', "rebase and merge, using the PR as merge commit message"),
        ('rebase-ff', "rebase and fast-forward"),
        ('squash', "squash"),
    ], default=False)
    method_warned = fields.Boolean(default=False)

    reviewed_by = fields.Many2one('res.partner')
    delegates = fields.Many2many('res.partner', help="Delegate reviewers, not intrinsically reviewers but can review this PR")
    priority = fields.Integer(default=2, index=True)

    overrides = fields.Char(required=True, default='{}')
    statuses = fields.Text(
        compute='_compute_statuses',
        help="Copy of the statuses from the HEAD commit, as a Python literal"
    )
    statuses_full = fields.Text(
        compute='_compute_statuses',
        help="Compilation of the full status of the PR (commit statuses + overrides), as JSON"
    )
    status = fields.Char(compute='_compute_statuses')
    previous_failure = fields.Char(default='{}')

    batch_id = fields.Many2one('runbot_merge.batch', string="Active Batch", compute='_compute_active_batch', store=True)
    batch_ids = fields.Many2many('runbot_merge.batch', string="Batches", context={'active_test': False})
    staging_id = fields.Many2one(related='batch_id.staging_id', store=True)
    commits_map = fields.Char(help="JSON-encoded mapping of PR commits to actually integrated commits. The integration head (either a merge commit or the PR's topmost) is mapped from the 'empty' pr commit (the key is an empty string, because you can't put a null key in json maps).", default='{}')

    link_warned = fields.Boolean(
        default=False, help="Whether we've already warned that this (ready)"
                            " PR is linked to an other non-ready PR"
    )

    blocked = fields.Char(
        compute='_compute_is_blocked',
        help="PR is not currently stageable for some reason (mostly an issue if status is ready)"
    )

    url = fields.Char(compute='_compute_url')
    github_url = fields.Char(compute='_compute_url')

    @api.depends('repository.name', 'number')
    def _compute_url(self):
        base = werkzeug.urls.url_parse(self.env['ir.config_parameter'].sudo().get_param('web.base.url', 'http://localhost:8069'))
        gh_base = werkzeug.urls.url_parse('https://github.com')
        for pr in self:
            path = f'/{werkzeug.urls.url_quote(pr.repository.name)}/pull/{pr.number}'
            pr.url = str(base.join(path))
            pr.github_url = str(gh_base.join(path))

    @api.depends('repository.name', 'number')
    def _compute_display_name(self):
        return super(PullRequests, self)._compute_display_name()

    def name_get(self):
        return [
            (p.id, '%s#%d' % (p.repository.name, p.number))
            for p in self
        ]

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        if not name or operator != 'ilike':
            return super().name_search(name, args=args, operator=operator, limit=limit)
        bits = [[('label', 'ilike', name)]]
        if name.isdigit():
            bits.append([('number', '=', name)])
        if re.match(r'\w+#\d+$', name):
            repo, num = name.rsplit('#', 1)
            bits.append(['&', ('repository.name', 'ilike', repo), ('number', '=', int(num))])
        else:
            bits.append([('repository.name', 'ilike', name)])
        domain = expression.OR(bits)
        if args:
            domain = expression.AND([args, domain])
        return self.search(domain, limit=limit).sudo().name_get()

    @property
    def _approved(self):
        return self.state in ('approved', 'ready') or any(
            p.priority == 0
            for p in (self | self._linked_prs)
        )

    @property
    def _ready(self):
        return (self.squash or self.merge_method) and self._approved and self.status == 'success'

    @property
    def _linked_prs(self):
        if re.search(r':patch-\d+', self.label):
            return self.browse(())
        if self.state == 'merged':
            return self.with_context(active_test=False).batch_ids\
                   .filtered(lambda b: b.staging_id.state == 'success')\
                   .prs - self
        return self.search([
            ('target', '=', self.target.id),
            ('label', '=', self.label),
            ('state', 'not in', ('merged', 'closed')),
        ]) - self

    # missing link to other PRs
    @api.depends('priority', 'state', 'squash', 'merge_method', 'batch_id.active', 'label')
    def _compute_is_blocked(self):
        self.blocked = False
        for pr in self:
            if pr.state in ('merged', 'closed'):
                continue

            linked = pr._linked_prs
            # check if PRs are configured (single commit or merge method set)
            if not (pr.squash or pr.merge_method):
                pr.blocked = 'has no merge method'
                continue
            other_unset = next((p for p in linked if not (p.squash or p.merge_method)), None)
            if other_unset:
                pr.blocked = "linked PR %s has no merge method" % other_unset.display_name
                continue

            # check if any PR in the batch is p=0 and none is in error
            if any(p.priority == 0 for p in (pr | linked)):
                if pr.state == 'error':
                    pr.blocked = "in error"
                other_error = next((p for p in linked if p.state == 'error'), None)
                if other_error:
                    pr.blocked = "linked pr %s in error" % other_error.display_name
                # if none is in error then none is blocked because p=0
                # "unblocks" the entire batch
                continue

            if pr.state != 'ready':
                pr.blocked = 'not ready'
                continue

            unready = next((p for p in linked if p.state != 'ready'), None)
            if unready:
                pr.blocked = 'linked pr %s is not ready' % unready.display_name
                continue

    def _get_overrides(self):
        if self:
            return json.loads(self.overrides)
        return {}

    @api.depends('head', 'repository.status_ids', 'overrides')
    def _compute_statuses(self):
        Commits = self.env['runbot_merge.commit']
        for pr in self:
            c = Commits.search([('sha', '=', pr.head)])
            st = json.loads(c.statuses or '{}')
            statuses = {**st, **pr._get_overrides()}
            pr.statuses_full = json.dumps(statuses)
            if not statuses:
                pr.status = pr.statuses = False
                continue

            pr.statuses = pprint.pformat(st)

            st = 'success'
            for ci in pr.repository.status_ids._for_pr(pr):
                v = state_(statuses, ci.context) or 'pending'
                if v in ('error', 'failure'):
                    st = 'failure'
                    break
                if v == 'pending':
                    st = 'pending'
            pr.status = st

    @api.depends('batch_ids.active')
    def _compute_active_batch(self):
        for r in self:
            r.batch_id = r.batch_ids.filtered(lambda b: b.active)[:1]

    def _get_or_schedule(self, repo_name, number, target=None):
        repo = self.env['runbot_merge.repository'].search([('name', '=', repo_name)])
        if not repo:
            return

        if target and not repo.project_id._has_branch(target):
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': repo.id,
                'pull_request': number,
                'message': "I'm sorry. Branch `{}` is not within my remit.".format(target),
            })
            return

        pr = self.search([
            ('repository', '=', repo.id),
            ('number', '=', number,)
        ])
        if pr:
            return pr

        Fetch = self.env['runbot_merge.fetch_job']
        if Fetch.search([('repository', '=', repo.id), ('number', '=', number)]):
            return
        Fetch.create({
            'repository': repo.id,
            'number': number,
        })

    def _parse_command(self, commandstring):
        for m in re.finditer(
            r'(\S+?)(?:([+-])|=(\S*))?(?=\s|$)',
            commandstring,
        ):
            name, flag, param = m.groups()
            if name == 'r':
                name = 'review'
            if flag in ('+', '-'):
                yield name, flag == '+'
            elif name == 'delegate':
                if param:
                    for p in param.split(','):
                        yield 'delegate', p.lstrip('#@')
            elif name == 'override':
                if param:
                    for p in param.split(','):
                        yield 'override', p
            elif name in ('p', 'priority'):
                if param in ('0', '1', '2'):
                    yield ('priority', int(param))
            elif any(name == k for k, _ in type(self).merge_method.selection):
                yield ('method', name)
            else:
                yield name, param

    def _parse_commands(self, author, comment, login):
        """Parses a command string prefixed by Project::github_prefix.

        A command string can contain any number of space-separated commands:

        retry
          resets a PR in error mode to ready for staging
        r(eview)+/-
           approves or disapproves a PR (disapproving just cancels an approval)
        delegate+/delegate=<users>
          adds either PR author or the specified (github) users as
          authorised reviewers for this PR. ``<users>`` is a
          comma-separated list of github usernames (no @)
        p(riority)=2|1|0
          sets the priority to normal (2), pressing (1) or urgent (0).
          Lower-priority PRs are selected first and batched together.
        rebase+/-
          Whether the PR should be rebased-and-merged (the default) or just
          merged normally.
        """
        assert self, "parsing commands must be executed in an actual PR"

        (login, name) = (author.github_login, author.display_name) if author else (login, 'not in system')

        is_admin, is_reviewer, is_author = self._pr_acl(author)

        commands = [
            ps
            for m in self.repository.project_id._find_commands(comment['body'] or '')
            for ps in self._parse_command(m)
        ]

        if not commands:
            _logger.info("found no commands in comment of %s (%s) (%s)", author.github_login, author.display_name,
                 utils.shorten(comment['body'] or '', 50)
            )
            return 'ok'

        Feedback = self.env['runbot_merge.pull_requests.feedback']
        if not (is_author or any(cmd == 'override' for cmd, _ in commands)):
            # no point even parsing commands
            _logger.info("ignoring comment of %s (%s): no ACL to %s",
                          login, name, self.display_name)
            Feedback.create({
                'repository': self.repository.id,
                'pull_request': self.number,
                'message': "I'm sorry, @{}. I'm afraid I can't do that.".format(login)
            })
            return 'ignored'

        applied, ignored = [], []
        def reformat(command, param):
            if param is None:
                pstr = ''
            elif isinstance(param, bool):
                pstr = '+' if param else '-'
            elif isinstance(param, list):
                pstr = '=' + ','.join(param)
            else:
                pstr = '={}'.format(param)

            return '%s%s' % (command, pstr)
        msgs = []
        for command, param in commands:
            ok = False
            msg = []
            if command == 'retry':
                if is_author:
                    if self.state == 'error':
                        ok = True
                        self.state = 'ready'
                    else:
                        msg = "Retry makes no sense when the PR is not in error."
            elif command == 'check':
                if is_author:
                    self.env['runbot_merge.fetch_job'].create({
                        'repository': self.repository.id,
                        'number': self.number,
                    })
            elif command == 'review':
                if self.draft:
                    msg = "Draft PRs can not be approved."
                elif param and is_reviewer:
                    oldstate = self.state
                    newstate = RPLUS.get(self.state)
                    if not author.email:
                        msg = "I must know your email before you can review PRs. Please contact an administrator."
                    elif not newstate:
                        msg = "This PR is already reviewed, reviewing it again is useless."
                    else:
                        self.state = newstate
                        self.reviewed_by = author
                        ok = True
                    _logger.debug(
                        "r+ on %s by %s (%s->%s) status=%s message? %s",
                        self.display_name, author.github_login,
                        oldstate, newstate or oldstate,
                        self.status, self.status == 'failure'
                    )
                    if self.status == 'failure':
                        # the normal infrastructure is for failure and
                        # prefixes messages with "I'm sorry"
                        Feedback.create({
                            'repository': self.repository.id,
                            'pull_request': self.number,
                            'message': "@{}, you may want to rebuild or fix this PR as it has failed CI.".format(author.github_login),
                        })
                elif not param and is_author:
                    newstate = RMINUS.get(self.state)
                    if self.priority == 0 or newstate:
                        if newstate:
                            self.state = newstate
                        if self.priority == 0:
                            self.priority = 1
                            Feedback.create({
                                'repository': self.repository.id,
                                'pull_request': self.number,
                                'message': "PR priority reset to 1, as pull requests with priority 0 ignore review state.",
                            })
                        self.unstage("unreview (r-) by %s", author.github_login)
                        ok = True
                    else:
                        msg = "r- makes no sense in the current PR state."
            elif command == 'delegate':
                if is_reviewer:
                    ok = True
                    Partners = self.env['res.partner']
                    if param is True:
                        delegate = self.author
                    else:
                        delegate = Partners.search([('github_login', '=', param)]) or Partners.create({
                            'name': param,
                            'github_login': param,
                        })
                    delegate.write({'delegate_reviewer': [(4, self.id, 0)]})
            elif command == 'priority':
                if is_admin:
                    ok = True
                    self.priority = param
                    if param == 0:
                        self.target.active_staging_id.cancel(
                            "P=0 on %s by %s, unstaging target %s",
                            self.display_name,
                            author.github_login, self.target.name,
                        )
            elif command == 'method':
                if is_reviewer:
                    if param == 'squash' and not self.squash:
                        msg = "Squash can only be used with a single commit at this time."
                    else:
                        self.merge_method = param
                        ok = True
                        explanation = next(label for value, label in type(self).merge_method.selection if value == param)
                        Feedback.create({
                            'repository': self.repository.id,
                            'pull_request': self.number,
                            'message':"Merge method set to %s" % explanation
                        })
            elif command == 'override':
                overridable = author.override_rights\
                    .filtered(lambda r: not r.repository_id or (r.repository_id == self.repository))\
                    .mapped('context')
                if param in overridable:
                    self.overrides = json.dumps({
                        **json.loads(self.overrides),
                        param: {
                            'state': 'success',
                            'target_url': comment['html_url'],
                            'description': f"Overridden by @{author.github_login}",
                        },
                    })
                    c = self.env['runbot_merge.commit'].search([('sha', '=', self.head)])
                    if c:
                        c.to_check = True
                    else:
                        c.create({'sha': self.head, 'statuses': '{}'})
                    ok = True
                else:
                    msg = f"You are not allowed to override this status."
            else:
                # ignore unknown commands
                continue

            _logger.info(
                "%s %s(%s) on %s by %s (%s)",
                "applied" if ok else "ignored",
                command, param, self.display_name,
                author.github_login, author.display_name,
            )
            if ok:
                applied.append(reformat(command, param))
            else:
                ignored.append(reformat(command, param))
                msgs.append(msg or "You can't {}.".format(reformat(command, param)))
        msg = []
        if applied:
            msg.append('applied ' + ' '.join(applied))
        if ignored:
            ignoredstr = ' '.join(ignored)
            msg.append('ignored ' + ignoredstr)

        if msgs:
            msgs.insert(0, "I'm sorry, @{}.".format(login))
            Feedback.create({
                'repository': self.repository.id,
                'pull_request': self.number,
                'message': ' '.join(msgs),
            })
        return '\n'.join(msg)

    def _pr_acl(self, user):
        if not self:
            return ACL(False, False, False)

        is_admin = self.env['res.partner.review'].search_count([
            ('partner_id', '=', user.id),
            ('repository_id', '=', self.repository.id),
            ('review', '=', True) if self.author != user else ('self_review', '=', True),
        ]) == 1
        is_reviewer = is_admin or self in user.delegate_reviewer
        # TODO: should delegate reviewers be able to retry PRs?
        is_author = is_reviewer or self.author == user
        return ACL(is_admin, is_reviewer, is_author)

    def _validate(self, statuses):
        # could have two PRs (e.g. one open and one closed) at least
        # temporarily on the same head, or on the same head with different
        # targets
        failed = self.browse(())
        for pr in self:
            required = pr.repository.status_ids._for_pr(pr).mapped('context')
            sts = {**statuses, **pr._get_overrides()}

            success = True
            for ci in required:
                st = state_(sts, ci) or 'pending'
                if st == 'success':
                    continue

                success = False
                if st in ('error', 'failure'):
                    failed |= pr
                    pr._notify_ci_new_failure(ci, to_status(sts.get(ci.strip(), 'pending')))
            if success:
                oldstate = pr.state
                if oldstate == 'opened':
                    pr.state = 'validated'
                elif oldstate == 'approved':
                    pr.state = 'ready'
        return failed

    def _notify_ci_new_failure(self, ci, st):
        prev = json.loads(self.previous_failure)
        if prev.get('state'): # old-style previous-failure
            prev = {ci: prev}
        if not any(self._statuses_equivalent(st, v) for v in prev.values()):
            prev[ci] = st
            self.previous_failure = json.dumps(prev)
            self._notify_ci_failed(ci)

    def _notify_merged(self, gh, payload):
        deployment = gh('POST', 'deployments', json={
            'ref': self.head, 'environment': 'merge',
            'description': "Merge %s into %s" % (self.display_name, self.target.name),
            'task': 'merge',
            'auto_merge': False,
            'required_contexts': [],
        }).json()
        gh('POST', 'deployments/{}/statuses'.format(deployment['id']), json={
            'state': 'success',
            'target_url': 'https://github.com/{}/commit/{}'.format(
                self.repository.name,
                payload['sha'],
            ),
            'description': "Merged %s in %s at %s" % (
                self.display_name, self.target.name, payload['sha']
            )
        })

    def _statuses_equivalent(self, a, b):
        """ Check if two statuses are *equivalent* meaning the description field
        is ignored (check only state and target_url). This is because the
        description seems to vary even if the rest does not, and generates
        unnecessary notififcations as a result
        """
        return a.get('state') == b.get('state') \
           and a.get('target_url')  == b.get('target_url')

    def _notify_ci_failed(self, ci):
        # only report an issue of the PR is already approved (r+'d)
        if self.state == 'approved':
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': self.repository.id,
                'pull_request': self.number,
                'message': "%r failed on this reviewed PR." % ci,
            })

    def _auto_init(self):
        super(PullRequests, self)._auto_init()
        # incorrect index: unique(number, target, repository).
        tools.drop_index(self._cr, 'runbot_merge_unique_pr_per_target', self._table)
        # correct index:
        tools.create_unique_index(
            self._cr, 'runbot_merge_unique_pr_per_repo', self._table, ['repository', 'number'])
        self._cr.execute("CREATE INDEX IF NOT EXISTS runbot_merge_pr_head "
                         "ON runbot_merge_pull_requests "
                         "USING hash (head)")

    @property
    def _tagstate(self):
        if self.state == 'ready' and self.staging_id.heads:
            return 'staged'
        return self.state

    @api.model
    def create(self, vals):
        pr = super().create(vals)
        c = self.env['runbot_merge.commit'].search([('sha', '=', pr.head)])
        pr._validate(json.loads(c.statuses or '{}'))

        if pr.state not in ('closed', 'merged'):
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': pr.repository.id,
                'pull_request': pr.number,
                'message': f"[Pull request status dashboard]({pr.url}).",
            })
        return pr

    def _from_gh(self, description, author=None, branch=None, repo=None):
        if repo is None:
            repo = self.env['runbot_merge.repository'].search([
                ('name', '=', description['base']['repo']['full_name']),
            ])
        if branch is None:
            branch = self.env['runbot_merge.branch'].with_context(active_test=False).search([
                ('name', '=', description['base']['ref']),
                ('project_id', '=', repo.project_id.id),
            ])
        if author is None:
            author = self.env['res.partner'].search([
                ('github_login', '=', description['user']['login']),
            ], limit=1)

        message = description['title'].strip()
        body = description['body'] and description['body'].strip()
        if body:
            message += '\n\n' + body
        return self.env['runbot_merge.pull_requests'].create({
            'state': 'opened' if description['state'] == 'open' else 'closed',
            'number': description['number'],
            'label': repo._remap_label(description['head']['label']),
            'author': author.id,
            'target': branch.id,
            'repository': repo.id,
            'head': description['head']['sha'],
            'squash': description['commits'] == 1,
            'message': message,
            'draft': description['draft'],
        })

    def write(self, vals):
        if vals.get('squash'):
            vals['merge_method'] = False

        w = super().write(vals)

        newhead = vals.get('head')
        if newhead:
            c = self.env['runbot_merge.commit'].search([('sha', '=', newhead)])
            self._validate(json.loads(c.statuses or '{}'))
        return w

    def _check_linked_prs_statuses(self, commit=False):
        """ Looks for linked PRs where at least one of the PRs is in a ready
        state and the others are not, notifies the other PRs.

        :param bool commit: whether to commit the tnx after each comment
        """
        # similar to Branch.try_staging's query as it's a subset of that
        # other query's behaviour
        self.env.cr.execute("""
        SELECT
          array_agg(pr.id) AS match
        FROM runbot_merge_pull_requests pr
        WHERE
          -- exclude terminal states (so there's no issue when
          -- deleting branches & reusing labels)
              pr.state != 'merged'
          AND pr.state != 'closed'
        GROUP BY
            pr.target,
            CASE
                WHEN pr.label SIMILAR TO '%%:patch-[[:digit:]]+'
                    THEN pr.id::text
                ELSE pr.label
            END
        HAVING
          -- one of the batch's PRs should be ready & not marked
              bool_or(pr.state = 'ready' AND NOT pr.link_warned)
          -- one of the others should be unready
          AND bool_or(pr.state != 'ready')
          -- but ignore batches with one of the prs at p0
          AND bool_and(pr.priority != 0)
        """)
        for [ids] in self.env.cr.fetchall():
            prs = self.browse(ids)
            ready = prs.filtered(lambda p: p.state == 'ready')
            unready = (prs - ready).sorted(key=lambda p: (p.repository.name, p.number))

            for r in ready:
                self.env['runbot_merge.pull_requests.feedback'].create({
                    'repository': r.repository.id,
                    'pull_request': r.number,
                    'message': "Linked pull request(s) {} not ready. Linked PRs are not staged until all of them are ready.".format(
                        ', '.join(map(
                            '{0.display_name}'.format,
                            unready
                        ))
                    )
                })
                r.link_warned = True
                if commit:
                    self.env.cr.commit()

        # send feedback for multi-commit PRs without a merge_method (which
        # we've not warned yet)
        for r in self.search([
            ('state', '=', 'ready'),
            ('squash', '=', False),
            ('merge_method', '=', False),
            ('method_warned', '=', False),
        ]):
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': r.repository.id,
                'pull_request': r.number,
                'message': "Because this PR has multiple commits, I need to know how to merge it:\n\n" + ''.join(
                    '* `%s` to %s\n' % pair
                    for pair in type(self).merge_method.selection
                    if pair[0] != 'squash'
                )
            })
            r.method_warned = True
            if commit:
                self.env.cr.commit()

    def _parse_commit_message(self, message):
        """ Parses a commit message to split out the pseudo-headers (which
        should be at the end) from the body, and serialises back with a
        predefined pseudo-headers ordering.
        """
        return Message.from_message(message)

    def _is_mentioned(self, message, *, full_reference=False):
        """Returns whether ``self`` is mentioned in ``message```

        :param str | PullRequest message:
        :param bool full_reference: whether the repository name must be present
        :rtype: bool
        """
        if full_reference:
            pattern = fr'\b{re.escape(self.display_name)}\b'
        else:
            repository = self.repository.name # .replace('/', '\\/')
            pattern = fr'( |\b{repository})#{self.number}\b'
        return bool(re.search(pattern, message if isinstance(message, str) else message.message))

    def _build_merge_message(self, message, related_prs=()):
        # handle co-authored commits (https://help.github.com/articles/creating-a-commit-with-multiple-authors/)
        m = self._parse_commit_message(message)
        if not self._is_mentioned(message):
            m.body += '\n\ncloses {pr.display_name}'.format(pr=self)

        for r in related_prs:
            if not r._is_mentioned(message, full_reference=True):
                m.headers.add('Related', r.display_name)

        if self.reviewed_by:
            m.headers.add('signed-off-by', self.reviewed_by.formatted_email)

        return m

    def _add_self_references(self, commits):
        """Adds a footer reference to ``self`` to all ``commits`` if they don't
        already refer to the PR.
        """
        for c in (c['commit'] for c in commits):
            if not self._is_mentioned(c['message']):
                m = self._parse_commit_message(c['message'])
                m.headers.pop('Part-Of', None)
                m.headers.add('Part-Of', self.display_name)
                c['message'] = str(m)

    def _stage(self, gh, target, related_prs=()):
        # nb: pr_commits is oldest to newest so pr.head is pr_commits[-1]
        _, prdict = gh.pr(self.number)
        commits = prdict['commits']
        method = self.merge_method or ('rebase-ff' if commits == 1 else None)
        if commits > 50 and method.startswith('rebase'):
            raise exceptions.Unmergeable(self, "Rebasing 50 commits is too much.")
        if commits > 250:
            raise exceptions.Unmergeable(
                self, "Merging PRs of 250 or more commits is not supported "
                "(https://developer.github.com/v3/pulls/#list-commits-on-a-pull-request)"
            )
        pr_commits = gh.commits(self.number)
        for c in pr_commits:
            if not (c['commit']['author']['email'] and c['commit']['committer']['email']):
                raise exceptions.Unmergeable(
                    self,
                    f"All commits must have author and committer email, "
                    f"missing email on {c['sha']} indicates the authorship is "
                    f"most likely incorrect."
                )
        pr_head = pr_commits[-1]['sha']
        if pr_head != self.head:
            raise exceptions.Mismatch(self.head, pr_head, commits == 1)

        if self.reviewed_by and self.reviewed_by.name == self.reviewed_by.github_login:
            # XXX: find other trigger(s) to sync github name?
            gh_name = gh.user(self.reviewed_by.github_login)['name']
            if gh_name:
                self.reviewed_by.name = gh_name

        # update pr message in case an update was missed
        msg = f'{prdict["title"]}\n\n{prdict.get("body") or ""}'.strip()
        if self.message != msg:
            self.message = msg

        # NOTE: lost merge v merge/copy distinction (head being
        #       a merge commit reused instead of being re-merged)
        return method, getattr(self, '_stage_' + method.replace('-', '_'))(
            gh, target, pr_commits, related_prs=related_prs)

    def _stage_squash(self, gh, target, commits, related_prs=()):
        original_head = gh.head(target)
        msg = self._build_merge_message(self, related_prs=related_prs)
        [commit] = commits
        merge_tree = gh.merge(commit['sha'], target, 'temp')['tree']['sha']
        squashed = gh('post', 'git/commits', json={
            'message': str(msg),
            'tree': merge_tree,
            'author': commit['commit']['author'],
            'committer': commit['commit']['committer'],
            'parents': [original_head],
        }).json()['sha']
        gh.set_ref(target, squashed)
        self.commits_map = json.dumps({commit['sha']: squashed, '': squashed})
        return squashed

    def _stage_rebase_ff(self, gh, target, commits, related_prs=()):
        # updates head commit with PR number (if necessary) then rebases
        # on top of target
        msg = self._build_merge_message(commits[-1]['commit']['message'], related_prs=related_prs)
        commits[-1]['commit']['message'] = str(msg)
        self._add_self_references(commits[:-1])
        head, mapping = gh.rebase(self.number, target, commits=commits)
        self.commits_map = json.dumps({**mapping, '': head})
        return head

    def _stage_rebase_merge(self, gh, target, commits, related_prs=()):
        self._add_self_references(commits)
        h, mapping = gh.rebase(self.number, target, reset=True, commits=commits)
        msg = self._build_merge_message(self, related_prs=related_prs)
        merge_head = gh.merge(h, target, str(msg))['sha']
        self.commits_map = json.dumps({**mapping, '': merge_head})
        return merge_head

    def _stage_merge(self, gh, target, commits, related_prs=()):
        pr_head = commits[-1] # oldest to newest
        base_commit = None
        head_parents = {p['sha'] for p in pr_head['parents']}
        if len(head_parents) > 1:
            # look for parent(s?) of pr_head not in PR, means it's
            # from target (so we merged target in pr)
            merge = head_parents - {c['sha'] for c in commits}
            external_parents = len(merge)
            if external_parents > 1:
                raise exceptions.Unmergeable(
                    "The PR head can only have one parent from the base branch "
                    "(not part of the PR itself), found %d: %s" % (
                        external_parents,
                        ', '.join(merge)
                    ))
            if external_parents == 1:
                [base_commit] = merge

        commits_map = {c['sha']: c['sha'] for c in commits}
        if base_commit:
            # replicate pr_head with base_commit replaced by
            # the current head
            original_head = gh.head(target)
            merge_tree = gh.merge(pr_head['sha'], target, 'temp merge')['tree']['sha']
            new_parents = [original_head] + list(head_parents - {base_commit})
            msg = self._build_merge_message(pr_head['commit']['message'], related_prs=related_prs)
            copy = gh('post', 'git/commits', json={
                'message': str(msg),
                'tree': merge_tree,
                'author': pr_head['commit']['author'],
                'committer': pr_head['commit']['committer'],
                'parents': new_parents,
            }).json()
            gh.set_ref(target, copy['sha'])
            # merge commit *and old PR head* map to the pr head replica
            commits_map[''] = commits_map[pr_head['sha']] = copy['sha']
            self.commits_map = json.dumps(commits_map)
            return copy['sha']
        else:
            # otherwise do a regular merge
            msg = self._build_merge_message(self)
            merge_head = gh.merge(self.head, target, str(msg))['sha']
            # and the merge commit is the normal merge head
            commits_map[''] = merge_head
            self.commits_map = json.dumps(commits_map)
            return merge_head

    def unstage(self, reason, *args):
        """ If the PR is staged, cancel the staging. If the PR is split and
        waiting, remove it from the split (possibly delete the split entirely)
        """
        split_batches = self.with_context(active_test=False).mapped('batch_ids').filtered('split_id')
        if len(split_batches) > 1:
            _logger.warning("Found a PR linked with more than one split batch: %s (%s)", self, split_batches)
        for b in split_batches:
            if len(b.split_id.batch_ids) == 1:
                # only the batch of this PR -> delete split
                b.split_id.unlink()
            else:
                # else remove this batch from the split
                b.split_id = False

        self.staging_id.cancel(reason, *args)

    def _try_closing(self, by):
        # ignore if the PR is already being updated in a separate transaction
        # (most likely being merged?)
        self.env.cr.execute('''
        SELECT id, state FROM runbot_merge_pull_requests
        WHERE id = %s AND state != 'merged'
        FOR UPDATE SKIP LOCKED;
        ''', [self.id])
        if not self.env.cr.fetchone():
            return False

        self.env.cr.execute('''
        UPDATE runbot_merge_pull_requests
        SET state = 'closed'
        WHERE id = %s
        ''', [self.id])
        self.env.cr.commit()
        self.modified(['state'])
        self.unstage(
            "PR %s closed by %s",
            self.display_name,
            by
        )
        return True

# state changes on reviews
RPLUS = {
    'opened': 'approved',
    'validated': 'ready',
}
RMINUS = {
    'approved': 'opened',
    'ready': 'validated',
    'error': 'validated',
}

_TAGS = {
    False: set(),
    'opened': {'seen 🙂'},
}
_TAGS['validated'] = _TAGS['opened'] | {'CI 🤖'}
_TAGS['approved'] = _TAGS['opened'] | {'r+ 👌'}
_TAGS['ready'] = _TAGS['validated'] | _TAGS['approved']
_TAGS['staged'] = _TAGS['ready'] | {'merging 👷'}
_TAGS['merged'] = _TAGS['ready'] | {'merged 🎉'}
_TAGS['error'] = _TAGS['opened'] | {'error 🙅'}
_TAGS['closed'] = _TAGS['opened'] | {'closed 💔'}
ALL_TAGS = set.union(*_TAGS.values())

class Tagging(models.Model):
    """
    Queue of tag changes to make on PRs.

    Several PR state changes are driven by webhooks, webhooks should return
    quickly, performing calls to the Github API would *probably* get in the
    way of that. Instead, queue tagging changes into this table whose
    execution can be cron-driven.
    """
    _name = _description = 'runbot_merge.pull_requests.tagging'

    repository = fields.Many2one('runbot_merge.repository', required=True)
    # store the PR number (not id) as we need a Tagging for PR objects
    # being deleted (retargeted to non-managed branches)
    pull_request = fields.Integer()

    tags_remove = fields.Char(required=True, default='[]')
    tags_add = fields.Char(required=True, default='[]')

    def create(self, values):
        if values.pop('state_from', None):
            values['tags_remove'] = ALL_TAGS
        if 'state_to' in values:
            values['tags_add'] = _TAGS[values.pop('state_to')]
        if not isinstance(values.get('tags_remove', ''), str):
            values['tags_remove'] = json.dumps(list(values['tags_remove']))
        if not isinstance(values.get('tags_add', ''), str):
            values['tags_add'] = json.dumps(list(values['tags_add']))
        return super().create(values)

    def _send(self):
        # noinspection SqlResolve
        self.env.cr.execute("""
        SELECT
            t.repository as repo_id,
            t.pull_request as pr_number,
            array_agg(t.id) as ids,
            array_agg(t.tags_remove::json) as to_remove,
            array_agg(t.tags_add::json) as to_add
        FROM runbot_merge_pull_requests_tagging t
        GROUP BY t.repository, t.pull_request
        """)
        Repos = self.env['runbot_merge.repository']
        ghs = {}
        to_remove = []
        for repo_id, pr, ids, remove, add in self.env.cr.fetchall():
            repo = Repos.browse(repo_id)

            gh = ghs.get(repo)
            if not gh:
                gh = ghs[repo] = repo.github()

            # fold all grouped PRs'
            tags_remove, tags_add = set(), set()
            for minus, plus in zip(remove, add):
                tags_remove.update(minus)
                # need to remove minuses from to_add in case we get e.g.
                # -foo +bar; -bar +baz, if we don't remove the minus, we'll end
                # up with -foo +bar +baz instead of -foo +baz
                tags_add.difference_update(minus)
                tags_add.update(plus)

            try:
                gh.change_tags(pr, tags_remove, tags_add)
            except Exception:
                _logger.exception(
                    "Error while trying to change the tags of %s#%s from %s to %s",
                    repo.name, pr, remove, add,
                )
            else:
                to_remove.extend(ids)
        self.browse(to_remove).unlink()

class Feedback(models.Model):
    """ Queue of feedback comments to send to PR users
    """
    _name = _description = 'runbot_merge.pull_requests.feedback'

    repository = fields.Many2one('runbot_merge.repository', required=True)
    # store the PR number (not id) as we may want to send feedback to PR
    # objects on non-handled branches
    pull_request = fields.Integer()
    message = fields.Char()
    close = fields.Boolean()
    token_field = fields.Selection(
        [('github_token', "Mergebot")],
        default='github_token',
        string="Bot User",
        help="Token field (from repo's project) to use to post messages"
    )

    def _send(self):
        ghs = {}
        to_remove = []
        for f in self.search([]):
            repo = f.repository
            gh = ghs.get((repo, f.token_field))
            if not gh:
                gh = ghs[(repo, f.token_field)] = repo.github(f.token_field)

            try:
                message = f.message
                if f.close:
                    gh.close(f.pull_request)
                    try:
                        data = json.loads(message or '')
                    except json.JSONDecodeError:
                        pass
                    else:
                        pr_to_notify = self.env['runbot_merge.pull_requests'].search([
                            ('repository', '=', repo.id),
                            ('number', '=', f.pull_request),
                        ])
                        if pr_to_notify:
                            pr_to_notify._notify_merged(gh, data)
                            message = None
                if message:
                    gh.comment(f.pull_request, message)
            except Exception:
                _logger.exception(
                    "Error while trying to %s %s#%s (%s)",
                    'close' if f.close else 'send a comment to',
                    repo.name, f.pull_request,
                    utils.shorten(f.message, 200)
                )
            else:
                to_remove.append(f.id)
        self.browse(to_remove).unlink()

class Commit(models.Model):
    """Represents a commit onto which statuses might be posted,
    independent of everything else as commits can be created by
    statuses only, by PR pushes, by branch updates, ...
    """
    _name = _description = 'runbot_merge.commit'

    sha = fields.Char(required=True)
    statuses = fields.Char(help="json-encoded mapping of status contexts to states", default="{}")
    to_check = fields.Boolean(default=False)

    def create(self, values):
        values['to_check'] = True
        r = super(Commit, self).create(values)
        return r

    def write(self, values):
        values.setdefault('to_check', True)
        r = super(Commit, self).write(values)
        return r

    def _notify(self):
        Stagings = self.env['runbot_merge.stagings']
        PRs = self.env['runbot_merge.pull_requests']
        # chances are low that we'll have more than one commit
        for c in self.search([('to_check', '=', True)]):
            try:
                c.to_check = False
                st = json.loads(c.statuses)
                pr = PRs.search([('head', '=', c.sha)])
                if pr:
                    pr._validate(st)

                stagings = Stagings.search([('heads', 'ilike', c.sha)]).filtered(
                    lambda s, h=c.sha: any(
                        head == h
                        for repo, head in json.loads(s.heads).items()
                        if not repo.endswith('^')
                    )
                )
                if stagings:
                    stagings._validate()
            except Exception:
                _logger.exception("Failed to apply commit %s (%s)", c, c.sha)
                self.env.cr.rollback()
            else:
                self.env.cr.commit()

    _sql_constraints = [
        ('unique_sha', 'unique (sha)', 'no duplicated commit'),
    ]

    def _auto_init(self):
        res = super(Commit, self)._auto_init()
        self._cr.execute("""
            CREATE INDEX IF NOT EXISTS runbot_merge_unique_statuses
            ON runbot_merge_commit
            USING hash (sha)
        """)
        self._cr.execute("""
            CREATE INDEX IF NOT EXISTS runbot_merge_to_process
            ON runbot_merge_commit ((1)) WHERE to_check
        """)
        return res

class Stagings(models.Model):
    _name = _description = 'runbot_merge.stagings'

    target = fields.Many2one('runbot_merge.branch', required=True)

    batch_ids = fields.One2many(
        'runbot_merge.batch', 'staging_id',
        context={'active_test': False},
    )
    state = fields.Selection([
        ('success', 'Success'),
        ('failure', 'Failure'),
        ('pending', 'Pending'),
        ('cancelled', "Cancelled"),
        ('ff_failed', "Fast forward failed")
    ], default='pending')
    active = fields.Boolean(default=True)

    staged_at = fields.Datetime(default=fields.Datetime.now)
    timeout_limit = fields.Datetime(store=True, compute='_compute_timeout_limit')
    reason = fields.Text("Reason for final state (if any)")

    # seems simpler than adding yet another indirection through a model
    heads = fields.Char(required=True, help="JSON-encoded map of heads, one per repo in the project")
    head_ids = fields.Many2many('runbot_merge.commit', compute='_compute_statuses')

    statuses = fields.Binary(compute='_compute_statuses')

    @api.depends('heads')
    def _compute_statuses(self):
        """ Fetches statuses associated with the various heads, returned as
        (repo, context, state, url)
        """
        Commits = self.env['runbot_merge.commit']
        for st in self:
            heads = {
                head: repo for repo, head in json.loads(st.heads).items()
                if not repo.endswith('^')
            }
            commits = st.head_ids = Commits.search([('sha', 'in', list(heads.keys()))])
            st.statuses = [
                (
                    heads[commit.sha],
                    context,
                    status.get('state') or 'pending',
                    status.get('target_url') or ''
                )
                for commit in commits
                for context, st in json.loads(commit.statuses).items()
                for status in [to_status(st)]
            ]

    # only depend on staged_at as it should not get modified, but we might
    # update the CI timeout after the staging have been created and we
    # *do not* want to update the staging timeouts in that case
    @api.depends('staged_at')
    def _compute_timeout_limit(self):
        for st in self:
            st.timeout_limit = fields.Datetime.to_string(
                  fields.Datetime.from_string(st.staged_at)
                + datetime.timedelta(minutes=st.target.project_id.ci_timeout)
            )

    def _validate(self):
        Commits = self.env['runbot_merge.commit']
        for s in self:
            if s.state != 'pending':
                continue

            repos = {
                repo.name: repo
                for repo in self.env['runbot_merge.repository'].search([])
                    .having_branch(s.target)
            }
            # maps commits to the statuses they need
            required_statuses = [
                (head, repos[repo].status_ids._for_staging(s).mapped('context'))
                for repo, head in json.loads(s.heads).items()
                if not repo.endswith('^')
            ]
            # maps commits to their statuses
            cmap = {
                c.sha: json.loads(c.statuses)
                for c in Commits.search([('sha', 'in', [h for h, _ in required_statuses])])
            }

            update_timeout_limit = False
            st = 'success'
            for head, reqs in required_statuses:
                statuses = cmap.get(head) or {}
                for v in map(lambda n: state_(statuses, n), reqs):
                    if st == 'failure' or v in ('error', 'failure'):
                        st = 'failure'
                    elif v is None:
                        st = 'pending'
                    elif v == 'pending':
                        st = 'pending'
                        update_timeout_limit = True
                    else:
                        assert v == 'success'

            vals = {'state': st}
            if update_timeout_limit:
                vals['timeout_limit'] = fields.Datetime.to_string(datetime.datetime.now() + datetime.timedelta(minutes=s.target.project_id.ci_timeout))
                _logger.debug("%s got pending status, bumping timeout to %s (%s)", self, vals['timeout_limit'], cmap)
            s.write(vals)

    def action_cancel(self):
        self.cancel("explicitly cancelled by %s", self.env.user.display_name)
        return { 'type': 'ir.actions.act_window_close' }

    def cancel(self, reason, *args):
        self = self.filtered('active')
        if not self:
            return

        _logger.info("Cancelling staging %s: " + reason, self, *args)
        self.mapped('batch_ids').write({'active': False})
        self.write({
            'active': False,
            'state': 'cancelled',
            'reason': reason % args,
        })

    def fail(self, message, prs=None):
        _logger.info("Staging %s failed: %s", self, message)
        prs = prs or self.batch_ids.prs
        prs.write({'state': 'error'})
        for pr in prs:
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': pr.repository.id,
                'pull_request': pr.number,
                'message':"Staging failed: %s" % message
            })

        self.batch_ids.write({'active': False})
        self.write({
            'active': False,
            'state': 'failure',
            'reason': message,
        })

    def try_splitting(self):
        batches = len(self.batch_ids)
        if batches > 1:
            midpoint = batches // 2
            h, t = self.batch_ids[:midpoint], self.batch_ids[midpoint:]
            # NB: batches remain attached to their original staging
            sh = self.env['runbot_merge.split'].create({
                'target': self.target.id,
                'batch_ids': [(4, batch.id, 0) for batch in h],
            })
            st = self.env['runbot_merge.split'].create({
                'target': self.target.id,
                'batch_ids': [(4, batch.id, 0) for batch in t],
            })
            _logger.info("Split %s to %s (%s) and %s (%s)",
                         self, h, sh, t, st)
            self.batch_ids.write({'active': False})
            self.write({
                'active': False,
                'state': 'failure',
                'reason': self.reason if self.state == 'failure' else 'timed out'
            })
            return True

        # single batch => the staging is an unredeemable failure
        if self.state != 'failure':
            # timed out, just mark all PRs (wheee)
            self.fail('timed out (>{} minutes)'.format(self.target.project_id.ci_timeout))
            return False

        # try inferring which PR failed and only mark that one
        for repo, head in json.loads(self.heads).items():
            if repo.endswith('^'):
                continue

            required_statuses = set(
                self.env['runbot_merge.repository']
                    .search([('name', '=', repo)])
                    .status_ids
                    ._for_staging(self)
                    .mapped('context'))

            commit = self.env['runbot_merge.commit'].search([('sha', '=', head)])
            statuses = json.loads(commit.statuses or '{}')
            reason = next((
                ctx for ctx, result in statuses.items()
                if ctx in required_statuses
                if to_status(result).get('state') in ('error', 'failure')
            ), None)
            if not reason:
                continue

            pr = next((
                pr for pr in self.batch_ids.prs
                if pr.repository.name == repo
            ), None)

            status = to_status(statuses[reason])
            viewmore = ''
            if status.get('target_url'):
                viewmore = ' (view more at %(target_url)s)' % status
            if pr:
                self.fail("%s%s" % (reason, viewmore), pr)
            else:
                self.fail('%s on %s%s' % (reason, head, viewmore))
            return False

        # the staging failed but we don't have a specific culprit, fail
        # everything
        self.fail("unknown reason")

        return False

    def check_status(self):
        """
        Checks the status of an active staging:
        * merges it if successful
        * splits it if failed (or timed out) and more than 1 batch
        * marks the PRs as failed otherwise
        * ignores if pending (or cancelled or ff_failed but those should also
          be disabled)
        """
        logger = _logger.getChild('cron')
        if not self.active:
            logger.info("Staging %s is not active, ignoring status check", self)
            return

        logger.info("Checking active staging %s (state=%s)", self, self.state)
        project = self.target.project_id
        if self.state == 'success':
            gh = {repo.name: repo.github() for repo in project.repo_ids.having_branch(self.target)}
            staging_heads = json.loads(self.heads)
            self.env.cr.execute('''
            SELECT 1 FROM runbot_merge_pull_requests
            WHERE id in %s
            FOR UPDATE
            ''', [tuple(self.mapped('batch_ids.prs.id'))])
            try:
                self._safety_dance(gh, staging_heads)
            except exceptions.FastForwardError as e:
                logger.warning(
                    "Could not fast-forward successful staging on %s:%s",
                    e.args[0], self.target.name,
                    exc_info=True
                )
                self.write({
                    'state': 'ff_failed',
                    'reason': str(e.__cause__ or e.__context__ or e)
                })
            else:
                prs = self.mapped('batch_ids.prs')
                logger.info(
                    "%s FF successful, marking %s as merged",
                    self, prs
                )
                prs.write({'state': 'merged'})

                pseudobranch = None
                if self.target == project.branch_ids[:1]:
                    pseudobranch = project._next_freeze()

                for pr in prs:
                    self.env['runbot_merge.pull_requests.feedback'].create({
                        'repository': pr.repository.id,
                        'pull_request': pr.number,
                        'message': json.dumps({
                            'sha': json.loads(pr.commits_map)[''],
                        }),
                        'close': True,
                    })
                    if pseudobranch:
                        self.env['runbot_merge.pull_requests.tagging'].create({
                            'repository': pr.repository.id,
                            'pull_request': pr.number,
                            'tags_add': json.dumps([pseudobranch]),
                        })
            finally:
                self.batch_ids.write({'active': False})
                self.write({'active': False})
        elif self.state == 'failure' or self.is_timed_out():
            self.try_splitting()

    def is_timed_out(self):
        return fields.Datetime.from_string(self.timeout_limit) < datetime.datetime.now()

    def _safety_dance(self, gh, staging_heads):
        """ Reverting updates doesn't work if the branches are protected
        (because a revert is basically a force push). So we can update
        REPO_A, then fail to update REPO_B for some reason, and we're hosed.

        To try and make this issue less likely, do the safety dance:

        * First, perform a dry run using the tmp branches (which can be
          force-pushed and sacrificed), that way if somebody pushed directly
          to REPO_B during the staging we catch it. If we're really unlucky
          they could still push after the dry run but...
        * An other issue then is that the github call sometimes fails for no
          noticeable reason (e.g. network failure or whatnot), if it fails
          on REPO_B when REPO_A has already been updated things get pretty
          bad. In that case, wait a bit and retry for now. A more complex
          strategy (including disabling the branch entirely until somebody
          has looked at and fixed the issue) might be necessary.

        :returns: the last repo it tried to update (probably the one on which
                  it failed, if it failed)
        """
        # FIXME: would make sense for FFE to be richer, and contain the repo name
        repo_name = None
        tmp_target = 'tmp.' + self.target.name
        # first force-push the current targets to all tmps
        for repo_name in staging_heads.keys():
            if repo_name.endswith('^'):
                continue
            g = gh[repo_name]
            g.set_ref(tmp_target, g.head(self.target.name))
        # then attempt to FF the tmp to the staging
        for repo_name, head in staging_heads.items():
            if repo_name.endswith('^'):
                continue
            gh[repo_name].fast_forward(tmp_target, staging_heads.get(repo_name + '^') or head)
        # there is still a race condition here, but it's way
        # lower than "the entire staging duration"...
        first = True
        for repo_name, head in staging_heads.items():
            if repo_name.endswith('^'):
                continue

            for pause in [0.1, 0.3, 0.5, 0.9, 0]: # last one must be 0/falsy of we lose the exception
                try:
                    # if the staging has a $repo^ head, merge that,
                    # otherwise merge the regular (CI'd) head
                    gh[repo_name].fast_forward(
                        self.target.name,
                        staging_heads.get(repo_name + '^') or head
                    )
                except exceptions.FastForwardError:
                    # The GH API regularly fails us. If the failure does not
                    # occur on the first repository, retry a few times with a
                    # little pause.
                    if not first and pause:
                        time.sleep(pause)
                        continue
                    raise
                else:
                    break
            first = False
        return repo_name

class Split(models.Model):
    _name = _description = 'runbot_merge.split'

    target = fields.Many2one('runbot_merge.branch', required=True)
    batch_ids = fields.One2many('runbot_merge.batch', 'split_id', context={'active_test': False})

class Batch(models.Model):
    """ A batch is a "horizontal" grouping of *codependent* PRs: PRs with
    the same label & target but for different repositories. These are
    assumed to be part of the same "change" smeared over multiple
    repositories e.g. change an API in repo1, this breaks use of that API
    in repo2 which now needs to be updated.
    """
    _name = _description = 'runbot_merge.batch'

    target = fields.Many2one('runbot_merge.branch', required=True)
    staging_id = fields.Many2one('runbot_merge.stagings')
    split_id = fields.Many2one('runbot_merge.split')

    prs = fields.Many2many('runbot_merge.pull_requests')

    active = fields.Boolean(default=True)

    @api.constrains('target', 'prs')
    def _check_prs(self):
        for batch in self:
            repos = self.env['runbot_merge.repository']
            for pr in batch.prs:
                if pr.target != batch.target:
                    raise ValidationError("A batch and its PRs must have the same branch, got %s and %s" % (batch.target, pr.target))
                if pr.repository in repos:
                    raise ValidationError("All prs of a batch must have different target repositories, got a duplicate %s on %s" % (pr.repository, pr))
                repos |= pr.repository

    def stage(self, meta, prs):
        """
        Updates meta[*][head] on success

        :return: () or Batch object (if all prs successfully staged)
        """
        new_heads = {}
        for pr in prs:
            gh = meta[pr.repository]['gh']

            _logger.info(
                "Staging pr %s for target %s; squash=%s",
                pr.display_name, pr.target.name, pr.squash
            )

            target = 'tmp.{}'.format(pr.target.name)
            original_head = gh.head(target)
            try:
                try:
                    method, new_heads[pr] = pr._stage(gh, target, related_prs=(prs - pr))
                    _logger.info(
                        "Staged pr %s to %s by %s: %s -> %s",
                        pr.display_name, pr.target.name, method,
                        original_head, new_heads[pr]
                    )
                except Exception:
                    # reset the head which failed, as rebase() may have partially
                    # updated it (despite later steps failing)
                    gh.set_ref(target, original_head)
                    # then reset every previous update
                    for to_revert in new_heads.keys():
                        it = meta[to_revert.repository]
                        it['gh'].set_ref('tmp.{}'.format(to_revert.target.name), it['head'])
                    raise
            except github.MergeError:
                raise exceptions.MergeError(pr)
            except exceptions.Mismatch as e:
                old_head, new_head, to_squash = e.args
                pr.write({
                    'state': 'opened',
                    'squash': to_squash,
                    'head': new_head,
                })
                _logger.warning(
                    "head mismatch on %s: had %s but found %s",
                    pr.display_name, old_head, new_head
                )
                self.env['runbot_merge.pull_requests.feedback'].create({
                    'repository': pr.repository.id,
                    'pull_request': pr.number,
                    'message': "We apparently missed an update to this PR "
                               "and tried to stage it in a state which "
                               "might not have been approved. PR has been "
                               "updated to %s, please check and approve or "
                               "re-approve." % new_head
                })
                return self.env['runbot_merge.batch']

        # update meta to new heads
        for pr, head in new_heads.items():
            meta[pr.repository]['head'] = head
        return self.create({
            'target': prs[0].target.id,
            'prs': [(4, pr.id, 0) for pr in prs],
        })

class FetchJob(models.Model):
    _name = _description = 'runbot_merge.fetch_job'

    active = fields.Boolean(default=True)
    repository = fields.Many2one('runbot_merge.repository', required=True)
    number = fields.Integer(required=True)

    def _check(self, commit=False):
        """
        :param bool commit: commit after each fetch has been executed
        """
        while True:
            f = self.search([], limit=1)
            if not f:
                return

            self.env.cr.execute("SAVEPOINT runbot_merge_before_fetch")
            try:
                f.repository._load_pr(f.number)
            except Exception:
                self.env.cr.execute("ROLLBACK TO SAVEPOINT runbot_merge_before_fetch")
                _logger.exception("Failed to load pr %s, skipping it", f.number)
            finally:
                self.env.cr.execute("RELEASE SAVEPOINT runbot_merge_before_fetch")

            f.active = False
            if commit:
                self.env.cr.commit()

# The commit (and PR) statuses was originally a map of ``{context:state}``
# however it turns out to clarify error messages it'd be useful to have
# a bit more information e.g. a link to the CI's build info on failure and
# all that. So the db-stored statuses are now becoming a map of
# ``{ context: {state, target_url, description } }``. The issue here is
# there's already statuses stored in the db so we need to handle both
# formats, hence these utility functions)
def state_(statuses, name):
    """ Fetches the status state """
    name = name.strip()
    v = statuses.get(name)
    if isinstance(v, dict):
        return v.get('state')
    return v
def to_status(v):
    """ Converts old-style status values (just a state string) to new-style
    (``{state, target_url, description}``)

    :type v: str | dict
    :rtype: dict
    """
    if isinstance(v, dict):
        return v
    return {'state': v, 'target_url': None, 'description': None}

refline = re.compile(rb'([0-9a-f]{40}) ([^\0\n]+)(\0.*)?\n$')
ZERO_REF = b'0'*40
def parse_refs_smart(read):
    """ yields pkt-line data (bytes), or None for flush lines """
    def read_line():
        length = int(read(4), 16)
        if length == 0:
            return None
        return read(length - 4)

    header = read_line()
    assert header == b'# service=git-upload-pack\n', header
    sep = read_line()
    assert sep is None, sep
    # read lines until second delimiter
    for line in iter(read_line, None):
        if line.startswith(ZERO_REF):
            break # empty list (no refs)
        m = refline.match(line)
        yield m[1].decode(), m[2].decode()

BREAK = re.compile(r'''
    ^
    [ ]{0,3} # 0-3 spaces of indentation
    # followed by a sequence of three or more matching -, _, or * characters,
    # each followed optionally by any number of spaces or tabs
    # so needs to start with a _, - or *, then have at least 2 more such
    # interspersed with any number of spaces or tabs
    ([*_-])
    ([ \t]*\1){2,}
    [ \t]*
    $
''', flags=re.VERBOSE)
SETEX_UNDERLINE = re.compile(r'''
    ^
    [ ]{0,3} # no more than 3 spaces indentation
    [-=]+ # a sequence of = characters or a sequence of - characters
    [ ]* # any number of trailing spaces
    $
    # we don't care about "a line containing a single -" because we want to
    # disambiguate SETEX headings from thematic breaks, and thematic breaks have
    # 3+ -. Doesn't look like GH interprets `- - -` as a line so yay...
''', flags=re.VERBOSE)
HEADER = re.compile('^([A-Za-z-]+): (.*)$')
class Message:
    @classmethod
    def from_message(cls, msg):
        in_headers = True
        maybe_setex = None
        # creating from PR message -> remove content following break
        msg, handle_break = (msg, False) if isinstance(msg, str) else (msg.message, True)
        headers = []
        body = []
        # don't process the title (first line) of the commit message
        msg = msg.splitlines()
        for line in reversed(msg[1:]):
            if maybe_setex:
                # NOTE: actually slightly more complicated: it's a SETEX heading
                #       only if preceding line(s) can be interpreted as a
                #       paragraph so e.g. a title followed by a line of dashes
                #       would indeed be a break, but this should be good enough
                #       for now, if we need more we'll need a full-blown
                #       markdown parser probably
                if line: # actually a SETEX title -> add underline to body then process current
                    body.append(maybe_setex)
                else: # actually break, remove body then process current
                    body = []
                maybe_setex = None

            if not line:
                if not in_headers and body and body[-1]:
                    body.append(line)
                continue

            if handle_break and BREAK.match(line):
                if SETEX_UNDERLINE.match(line):
                    maybe_setex = line
                else:
                    body = []
                continue

            h = HEADER.match(line)
            if h:
                # c-a-b = special case from an existing test, not sure if actually useful?
                if in_headers or h.group(1).lower() == 'co-authored-by':
                    headers.append(h.groups())
                    continue

            body.append(line)
            in_headers = False

        # if there are non-title body lines, add a separation after the title
        if body and body[-1]:
            body.append('')
        body.append(msg[0])
        return cls('\n'.join(reversed(body)), Headers(reversed(headers)))

    def __init__(self, body, headers=None):
        self.body = body
        self.headers = headers or Headers()

    def __setattr__(self, name, value):
        # make sure stored body is always stripped
        if name == 'body':
            value = value and value.strip()
        super().__setattr__(name, value)

    def __str__(self):
        if not self.headers:
            return self.body + '\n'

        with io.StringIO(self.body) as msg:
            msg.write(self.body)
            msg.write('\n\n')
            # https://git.wiki.kernel.org/index.php/CommitMessageConventions
            # seems to mostly use capitalised names (rather than title-cased)
            keys = list(OrderedSet(k.capitalize() for k in self.headers.keys()))
            # c-a-b must be at the very end otherwise github doesn't see it
            keys.sort(key=lambda k: k == 'Co-authored-by')
            for k in keys:
                for v in self.headers.getlist(k):
                    msg.write(k)
                    msg.write(': ')
                    msg.write(v)
                    msg.write('\n')

            return msg.getvalue()

    def sub(self, pattern, repl, *, flags):
        """ Performs in-place replacements on the body
        """
        self.body = re.sub(pattern, repl, self.body, flags=flags)
