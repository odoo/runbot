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
import sys
import time

from itertools import takewhile

import requests
from werkzeug.datastructures import Headers

from odoo import api, fields, models, tools
from odoo.exceptions import ValidationError
from odoo.tools import OrderedSet

from .. import github, exceptions, controllers, utils

WAIT_FOR_VISIBILITY = [10, 10, 10, 10]

_logger = logging.getLogger(__name__)
class Project(models.Model):
    _name = 'runbot_merge.project'

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

    required_statuses = fields.Char(
        help="Comma-separated list of status contexts which must be "\
        "`success` for a PR or staging to be valid",
        default='legal/cla,ci/runbot'
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

    def _check_progress(self, commit=False):
        for project in self.search([]):
            for staging in project.mapped('branch_ids.active_staging_id'):
                staging.check_status()
                if commit:
                    self.env.cr.commit()

            for branch in project.branch_ids:
                branch.try_staging()
                if commit:
                    self.env.cr.commit()

        # I have no idea why this is necessary for tests to pass, the only
        # DB update done not through the ORM is when receiving a notification
        # that a PR has been closed
        self.invalidate_cache()

    def _send_feedback(self):
        Repos = self.env['runbot_merge.repository']
        ghs = {}
        # noinspection SqlResolve
        self.env.cr.execute("""
        SELECT
            t.repository as repo_id,
            t.pull_request as pr_number,
            array_agg(t.id) as ids,
            (array_agg(t.state_from ORDER BY t.id))[1] as state_from,
            (array_agg(t.state_to ORDER BY t.id DESC))[1] as state_to
        FROM runbot_merge_pull_requests_tagging t
        GROUP BY t.repository, t.pull_request
        """)
        to_remove = []
        for repo_id, pr, ids, from_, to_ in self.env.cr.fetchall():
            repo = Repos.browse(repo_id)
            to_tags = _TAGS[to_ or False]

            gh = ghs.get(repo)
            if not gh:
                gh = ghs[repo] = repo.github()

            try:
                gh.change_tags(pr, to_tags)
            except Exception:
                _logger.exception(
                    "Error while trying to change the tags of %s:%s from %s to %s",
                    repo.name, pr, _TAGS[from_ or False], to_tags,
                )
            else:
                to_remove.extend(ids)
        self.env['runbot_merge.pull_requests.tagging'].browse(to_remove).unlink()

        to_remove = []
        for f in self.env['runbot_merge.pull_requests.feedback'].search([]):
            repo = f.repository
            gh = ghs.get((repo, f.token_field))
            if not gh:
                gh = ghs[(repo, f.token_field)] = repo.github(f.token_field)

            try:
                message = f.message
                if f.close:
                    gh.close(f.pull_request)
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        pass
                    else:
                        pr_to_notify = self.env['runbot_merge.pull_requests'].search([
                            ('repository', '=', repo.id),
                            ('number', '=', f.pull_request),
                        ])
                        if pr_to_notify:
                            self._notify_pr_merged(gh, pr_to_notify, data)
                            message = None
                if message:
                    gh.comment(f.pull_request, message)
            except Exception:
                _logger.exception(
                    "Error while trying to %s %s:%s (%s)",
                    'close' if f.close else 'send a comment to',
                    repo.name, f.pull_request,
                    utils.shorten(f.message, 200)
                )
            else:
                to_remove.append(f.id)
        self.env['runbot_merge.pull_requests.feedback'].browse(to_remove).unlink()

    def _notify_pr_merged(self, gh, pr, payload):
        deployment = gh('POST', 'deployments', json={
            'ref': pr.head, 'environment': 'merge',
            'description': "Merge %s into %s" % (pr, pr.target.name),
            'task': 'merge',
            'auto_merge': False,
            'required_contexts': [],
        }).json()
        gh('POST', 'deployments/{}/statuses'.format(deployment['id']), json={
            'state': 'success',
            'target_url': 'https://github.com/{}/commit/{}'.format(
                pr.repository.name,
                payload['sha'],
            ),
            'description': "Merged %s in %s at %s" % (
                pr, pr.target.name, payload['sha']
            )
        })

    def is_timed_out(self, staging):
        return fields.Datetime.from_string(staging.timeout_limit) < datetime.datetime.now()

    def _check_fetch(self, commit=False):
        """
        :param bool commit: commit after each fetch has been executed
        """
        while True:
            f = self.env['runbot_merge.fetch_job'].search([], limit=1)
            if not f:
                return

            self.env.cr.execute("SAVEPOINT runbot_merge_before_fetch")
            try:
                f.repository._load_pr(f.number)
            except Exception:
                self.env.cr.execute("ROLLBACK TO SAVEPOINT runbot_merge_before_fetch")
                _logger.exception("Failed to load pr %s, skipping it", f.number)
            self.env.cr.execute("RELEASE SAVEPOINT runbot_merge_before_fetch")

            # commit after each fetched PR
            f.active = False
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

class Repository(models.Model):
    _name = 'runbot_merge.repository'

    name = fields.Char(required=True)
    project_id = fields.Many2one('runbot_merge.project', required=True)

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

        if not self.project_id._has_branch(pr['base']['ref']):
            _logger.info("Tasked with loading PR %d for un-managed branch %s, ignoring", pr['number'], pr['base']['ref'])
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': self.id,
                'pull_request': number,
                'message': "I'm sorry. Branch `{}` is not within my remit.".format(pr['base']['ref']),
            })
            return

        # if the PR is already loaded, check... if the heads match?
        pr_id = self.env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', pr['base']['repo']['full_name']),
            ('number', '=', pr['number']),
        ])
        if pr_id:
            # TODO: edited, maybe (requires crafting a 'changes' object)
            r = controllers.handle_pr(self.env, {
                'action': 'synchronize',
                'pull_request': pr,
            })
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': pr_id.repository.id,
                'pull_request': self.number,
                'message': r,
            })
            return

        controllers.handle_pr(self.env, {
            'action': 'opened',
            'pull_request': pr,
        })
        for st in gh.statuses(pr['head']['sha']):
            controllers.handle_status(self.env, st)
        # get and handle all comments
        for comment in gh.comments(number):
            controllers.handle_comment(self.env, {
                'action': 'created',
                'issue': issue,
                'sender': comment['user'],
                'comment': comment,
                'repository': {'full_name': self.name},
            })
        # get and handle all reviews
        for review in gh.reviews(number):
            controllers.handle_review(self.env, {
                'action': 'submitted',
                'review': review,
                'pull_request': pr,
                'repository': {'full_name': self.name},
            })

class Branch(models.Model):
    _name = 'runbot_merge.branch'
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

    def _stageable(self):
        # noinspection SqlResolve
        self.env.cr.execute("""
        SELECT
          min(pr.priority) as priority,
          array_agg(pr.id) AS match
        FROM runbot_merge_pull_requests pr
        LEFT JOIN runbot_merge_batch batch ON pr.batch_id = batch.id AND batch.active
        WHERE pr.target = any(%s)
          -- exclude terminal states (so there's no issue when
          -- deleting branches & reusing labels)
          AND pr.state != 'merged'
          AND pr.state != 'closed'
        GROUP BY
            CASE
                WHEN pr.label SIMILAR TO '%%:patch-[[:digit:]]+'
                    THEN pr.id::text
                ELSE pr.label
            END
        HAVING
            -- all PRs in a group need to specify their merge method
            bool_and(pr.squash or pr.merge_method IS NOT NULL)
            AND (
                (bool_or(pr.priority = 0) AND NOT bool_or(pr.state = 'error'))
                OR bool_and(pr.state = 'ready')
            )
        ORDER BY min(pr.priority), min(pr.id)
        """, [self.ids])
        # result: [(priority, [pr_id for repo in repos])]
        return self.env.cr.fetchall()

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

        PRs = self.env['runbot_merge.pull_requests']

        rows = self._stageable()
        priority = rows[0][0] if rows else -1
        if priority == 0 or priority == 1:
            # p=0 take precedence over all else
            # p=1 allows merging a fix inside / ahead of a split (e.g. branch
            # is broken or widespread false positive) without having to cancel
            # the existing staging
            batched_prs = [PRs.browse(pr_ids) for _, pr_ids in takewhile(lambda r: r[0] == priority, rows)]
        elif self.split_ids:
            split_ids = self.split_ids[0]
            logger.info("Found split of PRs %s, re-staging", split_ids.mapped('batch_ids.prs'))
            batched_prs = [batch.prs for batch in split_ids.batch_ids]
            split_ids.unlink()
        else: # p=2
            batched_prs = [PRs.browse(pr_ids) for _, pr_ids in takewhile(lambda r: r[0] == priority, rows)]

        if not batched_prs:
            return

        Batch = self.env['runbot_merge.batch']
        staged = Batch
        meta = {repo: {} for repo in self.project_id.repo_ids}
        for repo, it in meta.items():
            gh = it['gh'] = repo.github()
            it['head'] = gh.head(self.name)
            # create tmp staging branch
            gh.set_ref('tmp.{}'.format(self.name), it['head'])

        batch_limit = self.project_id.batch_limit
        for batch in batched_prs:
            if len(staged) >= batch_limit:
                break
            staged |= Batch.stage(meta, batch)

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
                trailer = '\n' + '\n'.join(
                    'Runbot-dependency: %s:%s' % (repo, h)
                    for repo, h in heads.items()
                    if not repo.endswith('^')
                )
            dummy_head = it['gh']('post', 'git/commits', json={
                'message': 'force rebuild\n\nuniquifier: %s%s' % (r, trailer),
                'tree': tree['sha'],
                'parents': [it['head']],
            }).json()

            # $repo is the head to check, $repo^ is the head to merge
            heads[repo.name + '^'] = it['head']
            heads[repo.name] = dummy_head['sha']

        # create actual staging object
        st = self.env['runbot_merge.stagings'].create({
            'target': self.id,
            'batch_ids': [(4, batch.id, 0) for batch in staged],
            'heads': json.dumps(heads)
        })
        # create staging branch from tmp
        token = self.project_id.github_token
        for r in self.project_id.repo_ids:
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


        # creating the staging doesn't trigger a write on the prs
        # and thus the ->staging taggings, so do that by hand
        Tagging = self.env['runbot_merge.pull_requests.tagging']
        for pr in st.mapped('batch_ids.prs'):
            Tagging.create({
                'pull_request': pr.number,
                'repository': pr.repository.id,
                'state_from': 'ready',
                'state_to': 'staged',
            })

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
    _name = 'runbot_merge.pull_requests'
    _order = 'number desc'

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
    squash = fields.Boolean(default=False)
    merge_method = fields.Selection([
        ('merge', "merge directly, using the PR as merge commit message"),
        ('rebase-merge', "rebase and merge, using the PR as merge commit message"),
        ('rebase-ff', "rebase and fast-forward"),
    ], default=False)
    method_warned = fields.Boolean(default=False)

    reviewed_by = fields.Many2one('res.partner')
    delegates = fields.Many2many('res.partner', help="Delegate reviewers, not intrinsically reviewers but can review this PR")
    priority = fields.Selection([
        (0, 'Urgent'),
        (1, 'Pressing'),
        (2, 'Normal'),
    ], default=2, index=True)

    statuses = fields.Text(compute='_compute_statuses')
    status = fields.Char(compute='_compute_statuses')
    previous_failure = fields.Char(default='{}')

    batch_id = fields.Many2one('runbot_merge.batch',compute='_compute_active_batch', store=True)
    batch_ids = fields.Many2many('runbot_merge.batch')
    staging_id = fields.Many2one(related='batch_id.staging_id', store=True)
    commits_map = fields.Char(help="JSON-encoded mapping of PR commits to actually integrated commits. The integration head (either a merge commit or the PR's topmost) is mapped from the 'empty' pr commit (the key is an empty string, because you can't put a null key in json maps).", default='{}')

    link_warned = fields.Boolean(
        default=False, help="Whether we've already warned that this (ready)"
                            " PR is linked to an other non-ready PR"
    )

    blocked = fields.Boolean(
        compute='_compute_is_blocked',
        help="PR is not currently stageable for some reason (mostly an issue if status is ready)"
    )

    @api.depends('repository.name', 'number')
    def _compute_display_name(self):
        return super(PullRequests, self)._compute_display_name()

    def name_get(self):
        return {
            p.id: '%s#%s' % (p.repository.name, p.number)
            for p in self
        }

    def __str__(self):
        if len(self) == 0:
            separator = ''
        elif len(self) == 1:
            separator = ' '
        else:
            separator = 's '
        return '<pull_request%s%s>' % (separator, ' '.join(
            '{0.id} ({0.display_name})'.format(p)
            for p in self
        ))

    # missing link to other PRs
    @api.depends('priority', 'state', 'squash', 'merge_method', 'batch_id.active', 'label')
    def _compute_is_blocked(self):
        stageable = {
            pr_id
            for _, pr_ids in self.mapped('target')._stageable()
            for pr_id in pr_ids
        }
        for pr in self:
            pr.blocked = pr.id not in stageable

    @api.depends('head', 'repository.project_id.required_statuses')
    def _compute_statuses(self):
        Commits = self.env['runbot_merge.commit']
        for s in self:
            c = Commits.search([('sha', '=', s.head)])
            if not (c and c.statuses):
                continue

            statuses = json.loads(c.statuses)
            s.statuses = pprint.pformat(statuses)

            st = 'success'
            for ci in s.repository.project_id.required_statuses.split(','):
                v = state_(statuses, ci) or 'pending'
                if v in ('error', 'failure'):
                    st = 'failure'
                    break
                if v == 'pending':
                    st = 'pending'
            s.status = st

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
            r'(\S+?)(?:([+-])|=(\S*))?(?:\s|$)',
            commandstring,
        ):
            name, flag, param = m.groups()
            if name in ('retry', 'check'):
                yield (name, None)
            elif name in ('r', 'review'):
                if flag == '+':
                    yield ('review', True)
                elif flag == '-':
                    yield ('review', False)
            elif name == 'delegate':
                if flag == '+':
                    yield ('delegate', True)
                elif param:
                    yield ('delegate', [
                        p.lstrip('#@')
                        for p in param.split(',')
                    ])
            elif name in ('p', 'priority'):
                if param in ('0', '1', '2'):
                    yield ('priority', int(param))
            elif any(name == k for k, _ in type(self).merge_method.selection):
                yield ('method', name)

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

        commands = dict(
            ps
            for m in self.repository.project_id._find_commands(comment)
            for ps in self._parse_command(m)
        )

        if not commands:
            _logger.info("found no commands in comment of %s (%s) (%s)", author.github_login, author.display_name,
                 utils.shorten(comment, 50)
            )
            return 'ok'

        Feedback = self.env['runbot_merge.pull_requests.feedback']
        if not is_author:
            # no point even parsing commands
            _logger.info("ignoring comment of %s (%s): no ACL to %s:%s",
                          login, name,
                          self.repository.name, self.number)
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
        for command, param in commands.items():
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
                if param and is_reviewer:
                    newstate = RPLUS.get(self.state)
                    if newstate:
                        self.state = newstate
                        self.reviewed_by = author
                        ok = True
                        if self.status == 'failure':
                            # the normal infrastructure is for failure and
                            # prefixes messages with "I'm sorry"
                            Feedback.create({
                                'repository': self.repository.id,
                                'pull_request': self.number,
                                'message': "You may want to rebuild or fix this PR as it has failed CI.",
                            })
                    else:
                        msg = "This PR is already reviewed, reviewing it again is useless."
                elif not param and is_author:
                    newstate = RMINUS.get(self.state)
                    if newstate:
                        self.state = newstate
                        self.unstage("unreview (r-) by %s", author.github_login)
                        ok = True
                    else:
                        msg = "r- makes no sense in the current PR state."
            elif command == 'delegate':
                if is_reviewer:
                    ok = True
                    Partners = delegates = self.env['res.partner']
                    if param is True:
                        delegates |= self.author
                    else:
                        for login in param:
                            delegates |= Partners.search([('github_login', '=', login)]) or Partners.create({
                                'name': login,
                                'github_login': login,
                            })
                    delegates.write({'delegate_reviewer': [(4, self.id, 0)]})
            elif command == 'priority':
                if is_admin:
                    ok = True
                    self.priority = param
                    if param == 0:
                        self.target.active_staging_id.cancel(
                            "P=0 on %s:%s by %s, unstaging target %s",
                            self.repository.name, self.number,
                            author.github_login, self.target.name,
                        )
            elif command == 'method':
                if is_admin:
                    self.merge_method = param
                    ok = True
                    explanation = next(label for value, label in type(self).merge_method.selection if value == param)
                    Feedback.create({
                        'repository': self.repository.id,
                        'pull_request': self.number,
                        'message':"Merge method set to %s" % explanation
                    })

            _logger.info(
                "%s %s(%s) on %s:%s by %s (%s)",
                "applied" if ok else "ignored",
                command, param,
                self.repository.name, self.number,
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

        is_admin = (user.reviewer and self.author != user) or (user.self_reviewer and self.author == user)
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
            required = filter(None, pr.repository.project_id.required_statuses.split(','))

            success = True
            for ci in required:
                st = state_(statuses, ci) or 'pending'
                if st == 'success':
                    continue

                success = False
                if st in ('error', 'failure'):
                    failed |= pr
                    pr._notify_ci_new_failure(ci, to_status(statuses.get(ci.strip(), 'pending')))
            if success:
                oldstate = pr.state
                if oldstate == 'opened':
                    pr.state = 'validated'
                elif oldstate == 'approved':
                    pr.state = 'ready'
        return failed

    def _notify_ci_new_failure(self, ci, st):
        # only sending notification if the newly failed status is different than
        # the old one
        prev = json.loads(self.previous_failure)
        if st != prev:
            self.previous_failure = json.dumps(st)
            self._notify_ci_failed(ci)

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
            self.env['runbot_merge.pull_requests.tagging'].create({
                'pull_request': pr.number,
                'repository': pr.repository.id,
                'state_from': False,
                'state_to': pr._tagstate,
            })
        return pr

    def _from_gh(self, description, author=None, branch=None, repo=None):
        if repo is None:
            repo = self.env['runbot_merge.repository'].search([
                ('name', '=', description['base']['repo']['full_name']),
            ])
        if branch is None:
            branch = self.env['runbot_merge.branch'].search([
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
            'number': description['number'],
            'label': description['head']['label'],
            'author': author.id,
            'target': branch.id,
            'repository': repo.id,
            'head': description['head']['sha'],
            'squash': description['commits'] == 1,
            'message': message,
        })

    @api.multi
    def write(self, vals):
        oldstate = { pr: pr._tagstate for pr in self }

        w = super().write(vals)

        newhead = vals.get('head')
        if newhead:
            c = self.env['runbot_merge.commit'].search([('sha', '=', newhead)])
            if c.statuses:
                self._validate(json.loads(c.statuses))

        for pr in self:
            before, after = oldstate[pr], pr._tagstate
            if after != before:
                self.env['runbot_merge.pull_requests.tagging'].create({
                    'pull_request': pr.number,
                    'repository': pr.repository.id,
                    'state_from': oldstate[pr],
                    'state_to': pr._tagstate,
                })
        return w

    @api.multi
    def unlink(self):
        for pr in self:
            self.env['runbot_merge.pull_requests.tagging'].create({
                'pull_request': pr.number,
                'repository': pr.repository.id,
                'state_from': pr._tagstate,
                'state_to': False,
            })
        return super().unlink()

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
          -- but ignore batches with one of the prs at p-
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

    def _build_merge_message(self, message, related_prs=()):
        # handle co-authored commits (https://help.github.com/articles/creating-a-commit-with-multiple-authors/)
        m = self._parse_commit_message(message)
        pattern = r'( |{repository})#{pr.number}\b'.format(
            pr=self,
            repository=self.repository.name.replace('/', '\\/')
        )
        if not re.search(pattern, m.body):
            m.body += '\n\ncloses {pr.display_name}'.format(pr=self)

        for r in related_prs:
            m.headers.add('Related', r.display_name)

        if self.reviewed_by:
            m.headers.add('signed-off-by', self.reviewed_by.formatted_email)

        return str(m)

    def _stage(self, gh, target, related_prs=()):
        # nb: pr_commits is oldest to newest so pr.head is pr_commits[-1]
        _, prdict = gh.pr(self.number)
        commits = prdict['commits']
        method = self.merge_method or ('rebase-ff' if commits == 1 else None)
        assert commits < 50 or not method.startswith('rebase'), \
            "rebasing a PR of more than 50 commits is a tad excessive"
        assert commits < 250, "merging PRs of 250+ commits is not supported (https://developer.github.com/v3/pulls/#list-commits-on-a-pull-request)"
        pr_commits = gh.commits(self.number)
        pr_head = pr_commits[-1]['sha']
        if pr_head != self.head:
            raise exceptions.Skip(self.head, pr_head, commits == 1)

        if self.reviewed_by and self.reviewed_by.name == self.reviewed_by.github_login:
            # XXX: find other trigger(s) to sync github name?
            gh_name = gh.user(self.reviewed_by.github_login)['name']
            if gh_name:
                self.reviewed_by.name = gh_name

        # NOTE: lost merge v merge/copy distinction (head being
        #       a merge commit reused instead of being re-merged)
        return method, getattr(self, '_stage_' + method.replace('-', '_'))(
            gh, target, pr_commits, related_prs=related_prs)

    def _stage_rebase_ff(self, gh, target, commits, related_prs=()):
        # updates head commit with PR number (if necessary) then rebases
        # on top of target
        msg = self._build_merge_message(commits[-1]['commit']['message'], related_prs=related_prs)
        commits[-1]['commit']['message'] = msg
        head, mapping = gh.rebase(self.number, target, commits=commits)
        self.commits_map = json.dumps({**mapping, '': head})
        return head

    def _stage_rebase_merge(self, gh, target, commits, related_prs=()):
        msg = self._build_merge_message(self.message, related_prs=related_prs)
        h, mapping = gh.rebase(self.number, target, reset=True, commits=commits)
        merge_head = gh.merge(h, target, msg)['sha']
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
            assert len(merge) <= 1, \
                ">1 parent from base in PR's head is not supported"
            if len(merge) == 1:
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
                'message': msg,
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
            msg = self._build_merge_message(self.message)
            merge_head = gh.merge(self.head, target, msg)['sha']
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
        res = self.env.cr.fetchone()
        if not res:
            return False

        self.env.cr.execute('''
        UPDATE runbot_merge_pull_requests
        SET state = 'closed'
        WHERE id = %s AND state != 'merged'
        ''', [self.id])
        self.env.cr.commit()
        self.invalidate_cache(fnames=['state'], ids=[self.id])
        if self.env.cr.rowcount:
            self.env['runbot_merge.pull_requests.tagging'].create({
                'pull_request': self.number,
                'repository': self.repository.id,
                'state_from': res[1] if not self.staging_id else 'staged',
                'state_to': 'closed',
            })
            self.unstage(
                "PR %s:%s closed by %s",
                self.repository.name, self.number,
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

class Tagging(models.Model):
    """
    Queue of tag changes to make on PRs.

    Several PR state changes are driven by webhooks, webhooks should return
    quickly, performing calls to the Github API would *probably* get in the
    way of that. Instead, queue tagging changes into this table whose
    execution can be cron-driven.
    """
    _name = 'runbot_merge.pull_requests.tagging'

    repository = fields.Many2one('runbot_merge.repository', required=True)
    # store the PR number (not id) as we need a Tagging for PR objects
    # being deleted (retargeted to non-managed branches)
    pull_request = fields.Integer()

    state_from = fields.Selection([
        ('opened', 'Opened'),
        ('closed', 'Closed'),
        ('validated', 'Validated'),
        ('approved', 'Approved'),
        ('ready', 'Ready'),
        ('staged', 'Staged'),
        ('merged', 'Merged'),
        ('error', 'Error'),
    ])
    state_to = fields.Selection([
        ('opened', 'Opened'),
        ('closed', 'Closed'),
        ('validated', 'Validated'),
        ('approved', 'Approved'),
        ('ready', 'Ready'),
        ('staged', 'Staged'),
        ('merged', 'Merged'),
        ('error', 'Error'),
    ])

class Feedback(models.Model):
    """ Queue of feedback comments to send to PR users
    """
    _name = 'runbot_merge.pull_requests.feedback'

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

class Commit(models.Model):
    """Represents a commit onto which statuses might be posted,
    independent of everything else as commits can be created by
    statuses only, by PR pushes, by branch updates, ...
    """
    _name = 'runbot_merge.commit'

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
                # heads is a json-encoded mapping of reponame:head, so chances
                # are if a sha matches a heads it's matching one of the shas
                stagings = Stagings.search([('heads', 'ilike', c.sha)])
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
    _name = 'runbot_merge.stagings'

    target = fields.Many2one('runbot_merge.branch', required=True)

    batch_ids = fields.One2many(
        'runbot_merge.batch', 'staging_id',
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

            heads = [
                head for repo, head in json.loads(s.heads).items()
                if not repo.endswith('^')
            ]
            commits = Commits.search([
                ('sha', 'in', heads)
            ])

            update_timeout_limit = False
            reqs = [r.strip() for r in s.target.project_id.required_statuses.split(',')]
            st = 'success'
            for c in commits:
                statuses = json.loads(c.statuses)
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
            # mark failure as soon as we find a failed status, but wait until
            # all commits are known & not pending to mark a success
            if st == 'success' and len(commits) < len(heads):
                st = 'pending'

            vals = {'state': st}
            if update_timeout_limit:
                vals['timeout_limit'] = fields.Datetime.to_string(datetime.datetime.now() + datetime.timedelta(minutes=s.target.project_id.ci_timeout))
            s.write(vals)

    @api.multi
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
        _logger.error("Staging %s failed: %s", self, message)
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

            commit = self.env['runbot_merge.commit'].search([
                ('sha', '=', head)
            ])
            statuses = json.loads(commit.statuses or '{}')
            reason = next((
                ctx for ctx, result in statuses.items()
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
            gh = {repo.name: repo.github() for repo in project.repo_ids}
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
                    'reason': str(e.__cause__ or e.__context__ or '')
                })
            else:
                prs = self.mapped('batch_ids.prs')
                logger.info(
                    "%s FF successful, marking %s as merged",
                    self, prs
                )
                prs.write({'state': 'merged'})
                for pr in prs:
                    self.env['runbot_merge.pull_requests.feedback'].create({
                        'repository': pr.repository.id,
                        'pull_request': pr.number,
                        'message': json.dumps({
                            'sha': json.loads(pr.commits_map)[''],
                        }),
                        'close': True,
                    })
            finally:
                self.batch_ids.write({'active': False})
                self.write({'active': False})
        elif self.state == 'failure' or project.is_timed_out(self):
            self.try_splitting()

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
    _name = 'runbot_merge.split'

    target = fields.Many2one('runbot_merge.branch', required=True)
    batch_ids = fields.One2many('runbot_merge.batch', 'split_id', context={'active_test': False})

class Batch(models.Model):
    """ A batch is a "horizontal" grouping of *codependent* PRs: PRs with
    the same label & target but for different repositories. These are
    assumed to be part of the same "change" smeared over multiple
    repositories e.g. change an API in repo1, this breaks use of that API
    in repo2 which now needs to be updated.
    """
    _name = 'runbot_merge.batch'

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
                "Staging pr %s:%s for target %s; squash=%s",
                pr.repository.name, pr.number, pr.target.name, pr.squash
            )

            target = 'tmp.{}'.format(pr.target.name)
            original_head = gh.head(target)
            try:
                method, new_heads[pr] = pr._stage(gh, target, related_prs=(prs - pr))
                _logger.info(
                    "Staged pr %s:%s to %s by %s: %s -> %s",
                    pr.repository.name, pr.number,
                    pr.target.name, method,
                    original_head, new_heads[pr]
                )
            except (exceptions.MergeError, AssertionError) as e:
                if isinstance(e, exceptions.Skip):
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
                    msg = "We apparently missed an update to this PR and" \
                          " tried to stage it in a state which might not have" \
                          " been approved. PR has been updated to %s, please" \
                          " check and approve or re-approve." % new_head
                else:
                    _logger.exception("Failed to merge %s into staging branch (error: %s)",
                                      pr.display_name, e)
                    pr.state = 'error'
                    msg = "Unable to stage PR (%s)" % e

                self.env['runbot_merge.pull_requests.feedback'].create({
                    'repository': pr.repository.id,
                    'pull_request': pr.number,
                    'message': msg,
                })

                # reset the head which failed, as rebase() may have partially
                # updated it (despite later steps failing)
                gh.set_ref(target, original_head)
                # then reset every previous update
                for to_revert in new_heads.keys():
                    it = meta[to_revert.repository]
                    it['gh'].set_ref('tmp.{}'.format(to_revert.target.name), it['head'])

                return self.env['runbot_merge.batch']

        # update meta to new heads
        for pr, head in new_heads.items():
            meta[pr.repository]['head'] = head
        return self.create({
            'target': prs[0].target.id,
            'prs': [(4, pr.id, 0) for pr in prs],
        })

class FetchJob(models.Model):
    _name = 'runbot_merge.fetch_job'

    active = fields.Boolean(default=True)
    repository = fields.Many2one('runbot_merge.repository', required=True)
    number = fields.Integer(required=True)

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

HEADER = re.compile('^([A-Za-z-]+): (.*)$')
class Message:
    @classmethod
    def from_message(cls, msg):
        in_headers = True
        headers = []
        body = []
        for line in reversed(msg.splitlines()):
            if not line:
                if not in_headers and body and body[-1]:
                    body.append(line)
                continue

            h = HEADER.match(line)
            if h:
                # c-a-b = special case from an existing test, not sure if actually useful?
                if in_headers or h.group(1).lower() == 'co-authored-by':
                    headers.append(h.groups())
                    continue

            body.append(line)
            in_headers = False

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
