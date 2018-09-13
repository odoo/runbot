import base64
import collections
import datetime
import json
import logging
import os
import pprint
import re

from itertools import takewhile

from odoo import api, fields, models, tools
from odoo.exceptions import ValidationError

from .. import github, exceptions, controllers

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

    def _check_progress(self):
        logger = _logger.getChild('cron')
        Batch = self.env['runbot_merge.batch']
        PRs = self.env['runbot_merge.pull_requests']
        for project in self.search([]):
            gh = {repo.name: repo.github() for repo in project.repo_ids}
            # check status of staged PRs
            for staging in project.mapped('branch_ids.active_staging_id'):
                logger.info(
                    "Checking active staging %s (state=%s)",
                    staging, staging.state
                )
                if staging.state == 'success':
                    old_heads = {
                        n: g.head(staging.target.name)
                        for n, g in gh.items()
                    }
                    repo_name = None
                    staging_heads = json.loads(staging.heads)
                    updated = []
                    try:
                        for repo_name, head in staging_heads.items():
                            if repo_name.endswith('^'):
                                continue

                            # if the staging has a $repo^ head, merge that,
                            # otherwise merge the regular (CI'd) head
                            gh[repo_name].fast_forward(
                                staging.target.name,
                                staging_heads.get(repo_name + '^') or head
                            )
                            updated.append(repo_name)
                    except exceptions.FastForwardError:
                        logger.warning(
                            "Could not fast-forward successful staging on %s:%s, reverting updated repos %s and re-staging",
                            repo_name, staging.target.name,
                            ', '.join(updated),
                            exc_info=True
                        )
                        for name in reversed(updated):
                            gh[name].set_ref(staging.target.name, old_heads[name])
                    else:
                        prs = staging.mapped('batch_ids.prs')
                        logger.info(
                            "%s FF successful, marking %s as merged",
                            staging, prs
                        )
                        prs.write({'state': 'merged'})
                        for pr in prs:
                            # FIXME: this is the staging head rather than the actual merge commit for the PR
                            staging_head = staging_heads.get(pr.repository.name + '^') or staging_heads[pr.repository.name]
                            gh[pr.repository.name].close(pr.number, 'Merged in {}'.format(staging_head))
                    finally:
                        staging.batch_ids.write({'active': False})
                        staging.write({'active': False})
                elif staging.state == 'failure' or project.is_timed_out(staging):
                    staging.try_splitting()
                # else let flow

            # check for stageable branches/prs
            for branch in project.branch_ids:
                logger.info(
                    "Checking %s (%s) for staging: %s, skip? %s",
                    branch, branch.name,
                    branch.active_staging_id,
                    bool(branch.active_staging_id)
                )
                if branch.active_staging_id:
                    continue

                # noinspection SqlResolve
                self.env.cr.execute("""
                SELECT
                  min(pr.priority) as priority,
                  array_agg(pr.id) AS match
                FROM runbot_merge_pull_requests pr
                LEFT JOIN runbot_merge_batch batch ON pr.batch_id = batch.id AND batch.active
                WHERE pr.target = %s
                  -- exclude terminal states (so there's no issue when
                  -- deleting branches & reusing labels)
                  AND pr.state != 'merged'
                  AND pr.state != 'closed'
                GROUP BY pr.label
                HAVING (bool_or(pr.priority = 0) AND NOT bool_or(pr.state = 'error'))
                    OR bool_and(pr.state = 'ready')
                ORDER BY min(pr.priority), min(pr.id)
                """, [branch.id])
                # result: [(priority, [(repo_id, pr_id) for repo in repos]
                rows = self.env.cr.fetchall()
                priority = rows[0][0] if rows else -1
                if priority == 0:
                    # p=0 take precedence over all else
                    batched_prs = [
                        PRs.browse(pr_ids)
                        for _, pr_ids in takewhile(lambda r: r[0] == priority, rows)
                    ]
                elif branch.split_ids:
                    split_ids = branch.split_ids[0]
                    logger.info("Found split of PRs %s, re-staging", split_ids.mapped('batch_ids.prs'))
                    batched_prs = [batch.prs for batch in split_ids.batch_ids]
                    split_ids.unlink()
                elif rows:
                    # p=1 or p=2
                    batched_prs = [PRs.browse(pr_ids) for _, pr_ids in takewhile(lambda r: r[0] == priority, rows)]
                else:
                    continue

                staged = Batch
                meta = {repo: {} for repo in project.repo_ids}
                for repo, it in meta.items():
                    gh = it['gh'] = repo.github()
                    it['head'] = gh.head(branch.name)
                    # create tmp staging branch
                    gh.set_ref('tmp.{}'.format(branch.name), it['head'])

                batch_limit = project.batch_limit
                for batch in batched_prs:
                    if len(staged) >= batch_limit:
                        break
                    staged |= Batch.stage(meta, batch)

                if staged:
                    heads = {}
                    for repo, it in meta.items():
                        tree = it['gh'].commit(it['head'])['tree']
                        # ensures staging branches are unique and always
                        # rebuilt
                        r = base64.b64encode(os.urandom(12)).decode('ascii')
                        dummy_head = it['gh']('post', 'git/commits', json={
                            'message': 'force rebuild\n\nuniquifier: %s' % r,
                            'tree': tree['sha'],
                            'parents': [it['head']],
                        }).json()

                        # $repo is the head to check, $repo^ is the head to merge
                        heads[repo.name + '^'] = it['head']
                        heads[repo.name] = dummy_head['sha']

                    # create actual staging object
                    st = self.env['runbot_merge.stagings'].create({
                        'target': branch.id,
                        'batch_ids': [(4, batch.id, 0) for batch in staged],
                        'heads': json.dumps(heads)
                    })
                    # create staging branch from tmp
                    for r, it in meta.items():
                        it['gh'].set_ref('staging.{}'.format(branch.name), heads[r.name])

                    # creating the staging doesn't trigger a write on the prs
                    # and thus the ->staging taggings, so do that by hand
                    Tagging = self.env['runbot_merge.pull_requests.tagging']
                    for pr in st.mapped('batch_ids.prs'):
                        Tagging.create({
                            'pull_request': pr.number,
                            'repository': pr.repository.id,
                            'state_from': pr._tagstate,
                            'state_to': 'staged',
                        })

                    logger.info("Created staging %s (%s)", st, staged)

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
            from_tags = _TAGS[from_ or False]
            to_tags = _TAGS[to_ or False]

            gh = ghs.get(repo)
            if not gh:
                gh = ghs[repo] = repo.github()

            try:
                gh.change_tags(pr, from_tags, to_tags)
            except Exception:
                _logger.exception(
                    "Error while trying to change the tags of %s:%s from %s to %s",
                    repo.name, pr, from_tags, to_tags,
                )
            else:
                to_remove.extend(ids)
        self.env['runbot_merge.pull_requests.tagging'].browse(to_remove).unlink()

    def is_timed_out(self, staging):
        return fields.Datetime.from_string(staging.staged_at) + datetime.timedelta(minutes=self.ci_timeout) < datetime.datetime.now()

    def _check_fetch(self, commit=False):
        """
        :param bool commit: commit after each fetch has been executed
        """
        while True:
            f = self.env['runbot_merge.fetch_job'].search([], limit=1)
            if not f:
                return

            f.repository._load_pr(f.number)

            # commit after each fetched PR
            f.active = False
            if commit:
                self.env.cr.commit()

    def _find_commands(self, comment):
        return re.findall(
            '^{}:? (.*)$'.format(self.github_prefix),
            comment, re.MULTILINE)

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

    def github(self):
        return github.GH(self.project_id.github_token, self.name)

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
                'issue': issue,
                'sender': comment['user'],
                'comment': comment,
                'repository': {'full_name': self.name},
            })
        # get and handle all reviews
        for review in gh.reviews(number):
            controllers.handle_review(self.env, {
                'review': review,
                'pull_request': pr,
                'repository': {'full_name': self.name},
            })

class Branch(models.Model):
    _name = 'runbot_merge.branch'

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

    def _auto_init(self):
        res = super(Branch, self)._auto_init()
        tools.create_unique_index(
            self._cr, 'runbot_merge_unique_branch_per_repo',
            self._table, ['name', 'project_id'])
        return res

    @api.depends('staging_ids.active')
    def _compute_active_staging(self):
        for b in self:
            b.active_staging_id = b.staging_ids

class PullRequests(models.Model):
    _name = 'runbot_merge.pull_requests'
    _order = 'number desc'

    target = fields.Many2one('runbot_merge.branch', required=True)
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
    ], default='opened')

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
    rebase = fields.Boolean(default=True)

    delegates = fields.Many2many('res.partner', help="Delegate reviewers, not intrinsically reviewers but can review this PR")
    priority = fields.Selection([
        (0, 'Urgent'),
        (1, 'Pressing'),
        (2, 'Normal'),
    ], default=2, index=True)

    statuses = fields.Text(compute='_compute_statuses')

    batch_id = fields.Many2one('runbot_merge.batch',compute='_compute_active_batch', store=True)
    batch_ids = fields.Many2many('runbot_merge.batch')
    staging_id = fields.Many2one(related='batch_id.staging_id', store=True)

    @api.depends('head')
    def _compute_statuses(self):
        Commits = self.env['runbot_merge.commit']
        for s in self:
            c = Commits.search([('sha', '=', s.head)])
            if c and c.statuses:
                s.statuses = pprint.pformat(json.loads(c.statuses))

    @api.depends('batch_ids.active')
    def _compute_active_batch(self):
        for r in self:
            r.batch_id = r.batch_ids.filtered(lambda b: b.active)[:1]

    def _get_or_schedule(self, repo_name, number, target=None):
        repo = self.env['runbot_merge.repository'].search([('name', '=', repo_name)])
        if not repo:
            return

        if target and not repo.project_id._has_branch(target):
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
        m = re.match(r'(\w+)(?:([+-])|=(.*))?', commandstring)
        if not m:
            return None

        name, flag, param = m.groups()
        if name == 'retry':
            return ('retry', True)
        elif name in ('r', 'review'):
            if flag == '+':
                return ('review', True)
            elif flag == '-':
                return ('review', False)
        elif name == 'delegate':
            if flag == '+':
                return ('delegate', True)
            elif param:
                return ('delegate', param.split(','))
        elif name in ('p', 'priority'):
            if param in ('0', '1', '2'):
                return ('priority', int(param))
        elif name == 'rebase':
            return ('rebase', flag != '-')

        return None

    def _parse_commands(self, author, comment):
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

        is_admin = (author.reviewer and self.author != author) or (author.self_reviewer and self.author == author)
        is_reviewer = is_admin or self in author.delegate_reviewer
        # TODO: should delegate reviewers be able to retry PRs?
        is_author = is_reviewer or self.author == author

        if not is_author:
            # no point even parsing commands
            _logger.info("ignoring comment of %s (%s): no ACL to %s:%s",
                          author.github_login, author.display_name,
                          self.repository.name, self.number)
            return 'ignored'

        commands = dict(
            ps
            for m in self.repository.project_id._find_commands(comment)
            for c in m.strip().split()
            for ps in [self._parse_command(c)]
            if ps is not None
        )

        if not commands:
            _logger.info("found no commands in comment of %s (%s) (%s%s)", author.github_login, author.display_name,
                 comment[:50], '...' if len(comment) > 50 else ''
            )
            return 'ok'

        applied, ignored = [], []
        for command, param in commands.items():
            ok = False
            if command == 'retry':
                if is_author and self.state == 'error':
                    ok = True
                    self.state = 'ready'
            elif command == 'review':
                if param and is_reviewer:
                    if self.state == 'opened':
                        ok = True
                        self.state = 'approved'
                    elif self.state == 'validated':
                        ok = True
                        self.state = 'ready'
                elif not param and is_author and self.state == 'error':
                    # TODO: r- on something which isn't in error?
                    ok = True
                    self.state = 'validated'
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
                            "P=0 on %s:%s by %s, unstaging %s",
                            self.repository.name, self.number,
                            author.github_login, self.target.name,
                        )
            elif command == 'rebase':
                # anyone can rebase- their PR I guess?
                self.rebase = param

            _logger.info(
                "%s %s(%s) on %s:%s by %s (%s)",
                "applied" if ok else "ignored",
                command, param,
                self.repository.name, self.number,
                author.github_login, author.display_name,
            )
            if ok:
                applied.append('{}({})'.format(command, param))
            else:
                ignored.append('{}({})'.format(command, param))
        msg = []
        if applied:
            msg.append('applied ' + ' '.join(applied))
        if ignored:
            msg.append('ignored ' + ' '.join(ignored))
        return '\n'.join(msg)

    def _validate(self, statuses):
        # could have two PRs (e.g. one open and one closed) at least
        # temporarily on the same head, or on the same head with different
        # targets
        for pr in self:
            required = pr.repository.project_id.required_statuses.split(',')
            if all(statuses.get(r.strip()) == 'success' for r in required):
                oldstate = pr.state
                if oldstate == 'opened':
                    pr.state = 'validated'
                elif oldstate == 'approved':
                    pr.state = 'ready'

            #     _logger.info("CI+ (%s) for PR %s:%s: %s -> %s",
            #                  statuses, pr.repository.name, pr.number, oldstate, pr.state)
            # else:
            #     _logger.info("CI- (%s) for PR %s:%s", statuses, pr.repository.name, pr.number)

    def _auto_init(self):
        res = super(PullRequests, self)._auto_init()
        tools.create_unique_index(
            self._cr, 'runbot_merge_unique_pr_per_target', self._table, ['number', 'target', 'repository'])
        self._cr.execute("CREATE INDEX IF NOT EXISTS runbot_merge_pr_head "
                         "ON runbot_merge_pull_requests "
                         "USING hash (head)")
        return res

    @property
    def _tagstate(self):
        if self.state == 'ready' and self.staging_id.heads:
            return 'staged'
        return self.state

    @api.model
    def create(self, vals):
        pr = super().create(vals)
        c = self.env['runbot_merge.commit'].search([('sha', '=', pr.head)])
        if c and c.statuses:
            pr._validate(json.loads(c.statuses))

        if pr.state not in ('closed', 'merged'):
            self.env['runbot_merge.pull_requests.tagging'].create({
                'pull_request': pr.number,
                'repository': pr.repository.id,
                'state_from': False,
                'state_to': pr._tagstate,
            })
        return pr

    @api.multi
    def write(self, vals):
        oldstate = { pr: pr._tagstate for pr in self }
        w = super().write(vals)
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

class Commit(models.Model):
    """Represents a commit onto which statuses might be posted,
    independent of everything else as commits can be created by
    statuses only, by PR pushes, by branch updates, ...
    """
    _name = 'runbot_merge.commit'

    sha = fields.Char(required=True)
    statuses = fields.Char(help="json-encoded mapping of status contexts to states", default="{}")

    def create(self, values):
        r = super(Commit, self).create(values)
        r._notify()
        return r

    def write(self, values):
        r = super(Commit, self).write(values)
        self._notify()
        return r

    # NB: GH recommends doing heavy work asynchronously, may be a good
    #     idea to defer this to a cron or something
    def _notify(self):
        Stagings = self.env['runbot_merge.stagings']
        PRs = self.env['runbot_merge.pull_requests']
        # chances are low that we'll have more than one commit
        for c in self:
            st = json.loads(c.statuses)
            pr = PRs.search([('head', '=', c.sha)])
            if pr:
                pr._validate(st)
            # heads is a json-encoded mapping of reponame:head, so chances
            # are if a sha matches a heads it's matching one of the shas
            stagings = Stagings.search([('heads', 'ilike', c.sha)])
            if stagings:
                stagings._validate()

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
    ])
    active = fields.Boolean(default=True)

    staged_at = fields.Datetime(default=fields.Datetime.now)
    restaged = fields.Integer(default=0)

    # seems simpler than adding yet another indirection through a model
    heads = fields.Char(required=True, help="JSON-encoded map of heads, one per repo in the project")

    def _validate(self):
        Commits = self.env['runbot_merge.commit']
        for s in self:
            heads = [
                head for repo, head in json.loads(s.heads).items()
                if not repo.endswith('^')
            ]
            commits = Commits.search([
                ('sha', 'in', heads)
            ])

            reqs = [r.strip() for r in s.target.project_id.required_statuses.split(',')]
            st = 'success'
            for c in commits:
                statuses = json.loads(c.statuses)
                for v in map(statuses.get, reqs):
                    if st == 'failure' or v in ('error', 'failure'):
                        st = 'failure'
                    elif v in (None, 'pending'):
                        st = 'pending'
                    else:
                        assert v == 'success'

            # mark failure as soon as we find a failed status, but wait until
            # all commits are known & not pending to mark a success
            if st == 'success' and len(commits) < len(heads):
                s.state = 'pending'
                continue

            s.state = st

    def cancel(self, reason, *args):
        if not self:
            return

        _logger.info(reason, *args)
        self.batch_ids.write({'active': False})
        self.active = False

    def fail(self, message, prs=None):
        _logger.error("Staging %s failed: %s", self, message)
        prs = prs or self.batch_ids.prs
        prs.write({'state': 'error'})
        for pr in prs:
            pr.repository.github().comment(
                pr.number, "Staging failed: %s" % message)

        self.batch_ids.write({'active': False})
        self.active = False

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
            self.active = False
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
            reason = next((
                ctx for ctx, result in json.loads(commit.statuses).items()
                if result in ('error', 'failure')
            ), None)
            if not reason:
                continue

            pr = next((
                pr for pr in self.batch_ids.prs
                if pr.repository.name == repo
            ), None)
            if pr:
                self.fail(reason, pr)
            else:
                self.fail('%s failed on %s' % (reason, head))
            return False

        # the staging failed but we don't have a specific culprit, fail
        # everything
        self.fail("unknown reason")

        return False

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

        def build_message(message, pr):
            m = re.search(r'( |{repository})#{pr.number}\b'.format(
                    pr=pr, repository=pr.repository.name.replace('/', '\\/')),
                message)
            if m:
                return message
            return message + '\n\ncloses {pr.repository.name}#{pr.number}'.format(pr=pr)

        new_heads = {}
        for pr in prs:
            gh = meta[pr.repository]['gh']

            _logger.info(
                "Staging pr %s:%s for target %s; squash=%s",
                pr.repository.name, pr.number, pr.target.name, pr.squash
            )

            target = 'tmp.{}'.format(pr.target.name)
            try:
                # nb: pr_commits is oldest to newest so pr.head is pr_commits[-1]
                pr_commits = gh.commits(pr.number)
                rebase_and_merge = pr.rebase
                squash = rebase_and_merge and len(pr_commits) == 1
                if squash:
                    msg = build_message(pr_commits[0]['commit']['message'], pr)
                    pr_commits[0]['commit']['message'] = msg
                    new_heads[pr] = gh.rebase(pr.number, target, commits=pr_commits)
                elif rebase_and_merge:
                    msg = build_message(pr.message, pr)
                    h = gh.rebase(pr.number, target, reset=True, commits=pr_commits)
                    new_heads[pr] = gh.merge(h, target, msg)['sha']
                else:
                    pr_head = pr_commits[-1] # pr_commits is oldest to newest
                    base_commit = None
                    head_parents = {p['sha'] for p in pr_head['parents']}
                    if len(head_parents) > 1:
                        # look for parent(s?) of pr_head not in PR, means it's
                        # from target (so we merged target in pr)
                        merge = head_parents - {c['sha'] for c in pr_commits}
                        assert len(merge) <= 1, \
                            ">1 parent from base in PR's head is not supported"
                        if len(merge) == 1:
                            [base_commit] = merge

                    if base_commit:
                        # replicate pr_head with base_commit replaced by
                        # the current head
                        original_head = gh.head(target)
                        merge_tree = gh.merge(pr_head['sha'], target, 'temp merge')['tree']['sha']
                        new_parents = [original_head] + list(head_parents - {base_commit})
                        msg = build_message(pr_head['commit']['message'], pr)
                        copy = gh('post', 'git/commits', json={
                            'message': msg,
                            'tree': merge_tree,
                            'author': pr_head['commit']['author'],
                            'committer': pr_head['commit']['committer'],
                            'parents': new_parents,
                        }).json()
                        gh.set_ref(target, copy['sha'])
                        new_heads[pr] = copy['sha']
                    else:
                        # otherwise do a regular merge
                        msg = build_message(pr.message, pr)
                        new_heads[pr] = gh.merge(pr.head, target, msg)['sha']
            except (exceptions.MergeError, AssertionError) as e:
                _logger.exception("Failed to merge %s:%s into staging branch (error: %s)", pr.repository.name, pr.number, e)
                pr.state = 'error'
                gh.comment(pr.number, "Unable to stage PR (merge conflict)")

                # reset other PRs
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
    repository = fields.Many2one('runbot_merge.repository', index=True, required=True)
    number = fields.Integer(index=True, required=True)
