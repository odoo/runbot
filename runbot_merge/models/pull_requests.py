import collections
import datetime
import json
import logging
import pprint
import re

from itertools import takewhile

from odoo import api, fields, models, tools
from odoo.exceptions import ValidationError

from .. import github, exceptions

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
             "comments e.g. [hanson retry] or [hanson r+ p=1 squash+]"
    )

    batch_limit = fields.Integer(
        default=8, help="Maximum number of PRs staged together")

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
                            gh[repo_name].fast_forward(
                                staging.target.name,
                                head
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
                            gh[pr.repository.name].close(pr.number, 'Merged in {}'.format(staging_heads[pr.repository.name]))
                    finally:
                        staging.batch_ids.unlink()
                        staging.unlink()
                elif staging.state == 'failure' or project.is_timed_out(staging):
                    staging.try_splitting()
                # else let flow

            # check for stageable branches/prs
            for branch in project.branch_ids:
                logger.info(
                    "Checking %s (%s) for staging: %s, ignore? %s",
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
                WHERE pr.target = %s
                  AND pr.batch_id IS NULL
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
                    batches = [
                        PRs.browse(pr_ids)
                        for _, pr_ids in takewhile(lambda r: r[0] == priority, rows)
                    ]
                elif branch.staging_ids:
                    # Splits can generate inactive stagings, restage these first
                    staging = branch.staging_ids[0]
                    logger.info("Found inactive staging %s, reactivating", staging)
                    batches = [batch.prs for batch in staging.batch_ids]
                    staging.unlink()
                elif rows:
                    # p=1 or p=2
                    batches = [PRs.browse(pr_ids) for _, pr_ids in takewhile(lambda r: r[0] == priority, rows)]
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
                for batch in batches:
                    if len(staged) >= batch_limit:
                        break
                    staged |= Batch.stage(meta, batch)

                if staged:
                    # create actual staging object
                    st = self.env['runbot_merge.stagings'].create({
                        'target': branch.id,
                        'batch_ids': [(4, batch.id, 0) for batch in staged],
                        'heads': json.dumps({
                            repo.name: it['head']
                            for repo, it in meta.items()
                        })
                    })
                    # create staging branch from tmp
                    for r, it in meta.items():
                        it['gh'].set_ref('staging.{}'.format(branch.name), it['head'])

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

                    logger.info("Created staging %s", st)

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

    def sync_prs(self):
        _logger.info("Synchronizing PRs for %s", self.name)
        Commits = self.env['runbot_merge.commit']
        PRs = self.env['runbot_merge.pull_requests']
        Partners = self.env['res.partner']
        branches = {
            b.name: b
            for b in self.branch_ids
        }
        authors = {
            p.github_login: p
            for p in Partners.search([])
            if p.github_login
        }
        for repo in self.repo_ids:
            gh = repo.github()
            created = 0
            ignored_targets = collections.Counter()
            prs = {
                pr.number: pr
                for pr in PRs.search([
                    ('repository', '=', repo.id),
                ])
            }
            for i, pr in enumerate(gh.prs()):
                message = "{}\n\n{}".format(pr['title'].strip(), pr['body'].strip())
                existing = prs.get(pr['number'])
                target = pr['base']['ref']
                if existing:
                    if target not in branches:
                        _logger.info("PR %d retargeted to non-managed branch %s, deleting", pr['number'],
                                     target)
                        ignored_targets.update([target])
                        existing.unlink()
                    else:
                        if message != existing.message:
                            _logger.info("Updating PR %d ({%s} != {%s})", pr['number'], existing.message, message)
                            existing.message = message
                    continue

                # not for a selected target => skip
                if target not in branches:
                    ignored_targets.update([target])
                    continue

                # old PR, source repo may have been deleted, ignore
                if not pr['head']['label']:
                    _logger.info('ignoring PR %d: no label', pr['number'])
                    continue

                login = pr['user']['login']
                # no author on old PRs, account deleted
                author = authors.get(login, Partners)
                if login and not author:
                    author = authors[login] = Partners.create({
                        'name': login,
                        'github_login': login,
                    })
                head = pr['head']['sha']
                PRs.create({
                    'number': pr['number'],
                    'label': pr['head']['label'],
                    'author': author.id,
                    'target': branches[target].id,
                    'repository': repo.id,
                    'head': head,
                    'squash': pr['commits'] == 1,
                    'message': message,
                    'state': 'opened' if pr['state'] == 'open'
                    else 'merged' if pr.get('merged')
                    else 'closed'
                })
                c = Commits.search([('sha', '=', head)]) or Commits.create({'sha': head})
                c.statuses = json.dumps(pr['head']['statuses'])

                created += 1
            _logger.info("%d new prs in %s", created, repo.name)
            _logger.info('%d ignored PRs for un-managed targets: (%s)', sum(ignored_targets.values()), dict(ignored_targets))
        return False

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

class Branch(models.Model):
    _name = 'runbot_merge.branch'

    name = fields.Char(required=True)
    project_id = fields.Many2one('runbot_merge.project', required=True)

    active_staging_id = fields.One2many(
        'runbot_merge.stagings', 'target',
        domain=[("heads", "!=", False)],
        help="Currently running staging for the branch, there should be only one"
    )
    staging_ids = fields.One2many('runbot_merge.stagings', 'target')

    def _auto_init(self):
        res = super(Branch, self)._auto_init()
        tools.create_unique_index(
            self._cr, 'runbot_merge_unique_branch_per_repo',
            self._table, ['name', 'project_id'])
        return res

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
    head = fields.Char(required=True, index=True)
    label = fields.Char(
        required=True, index=True,
        help="Label of the source branch (owner:branchname), used for "
             "cross-repository branch-matching"
    )
    message = fields.Text(required=True)
    squash = fields.Boolean(default=False)

    delegates = fields.Many2many('res.partner', help="Delegate reviewers, not intrisically reviewers but can review this PR")
    priority = fields.Selection([
        (0, 'Urgent'),
        (1, 'Pressing'),
        (2, 'Normal'),
    ], default=2, index=True)

    statuses = fields.Text(compute='_compute_statuses')

    batch_id = fields.Many2one('runbot_merge.batch')
    staging_id = fields.Many2one(related='batch_id.staging_id', store=True)

    @api.depends('head')
    def _compute_statuses(self):
        Commits = self.env['runbot_merge.commit']
        for s in self:
            c = Commits.search([('sha', '=', s.head)])
            if c and c.statuses:
                s.statuses = pprint.pformat(json.loads(c.statuses))

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
        elif name == 'squash':
            if flag == '+':
                return ('squash', True)
            elif flag == '-':
                return ('squash', False)
        elif name == 'delegate':
            if flag == '+':
                return ('delegate', True)
            elif param:
                return ('delegate', param.split(','))
        elif name in ('p', 'priority'):
            if param in ('0', '1', '2'):
                return ('priority', int(param))

        return None

    def _parse_commands(self, author, comment):
        """Parses a command string prefixed by Project::github_prefix.

        A command string can contain any number of space-separated commands:

        retry
          resets a PR in error mode to ready for staging
        r(eview)+/-
           approves or disapproves a PR (disapproving just cancels an approval)
        squash+/squash-
          marks the PR as squash or merge, can override squash inference or a
          previous squash command
        delegate+/delegate=<users>
          adds either PR author or the specified (github) users as
          authorised reviewers for this PR. ``<users>`` is a
          comma-separated list of github usernames (no @)
        p(riority)=2|1|0
          sets the priority to normal (2), pressing (1) or urgent (0).
          Lower-priority PRs are selected first and batched together.
        """
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
            for m in re.findall('^{}:? (.*)$'.format(self.repository.project_id.github_prefix), comment, re.MULTILINE)
            for c in m.strip().split()
            for ps in [self._parse_command(c)]
            if ps is not None
        )

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

            elif command == 'squash':
                if is_admin:
                    ok = True
                    self.squash = param
            elif command == 'priority':
                if is_admin:
                    ok = True
                    self.priority = param
                    self.target.active_staging_id.cancel(
                        "P=0 on %s:%s by %s, unstaging %s",
                        self.repository.name, self.number,
                        author.github_login, self.target.name,
                    )

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
        return res

    @property
    def _tagstate(self):
        if self.state == 'ready' and self.staging_id.heads:
            return 'staged'
        return self.state

    @api.model
    def create(self, vals):
        pr = super().create(vals)
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

    repository = fields.Many2one('pull_request.repository', required=True)
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

    def _auto_init(self):
        res = super(Commit, self)._auto_init()
        tools.create_unique_index(
            self._cr, 'runbot_merge_unique_statuses', self._table, ['sha'])
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

    staged_at = fields.Datetime(default=fields.Datetime.now)
    restaged = fields.Integer(default=0)

    # seems simpler than adding yet another indirection through a model and
    # makes checking for actually staged stagings way easier: just see if
    # heads is set
    heads = fields.Char(help="JSON-encoded map of heads, one per repo in the project")

    def _validate(self):
        Commits = self.env['runbot_merge.commit']
        for s in self:
            heads = list(json.loads(s.heads).values())
            commits = Commits.search([
                ('sha', 'in', heads)
            ])
            if len(commits) < len(heads):
                s.state = 'pending'
                continue

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
            s.state = st

    def cancel(self, reason, *args):
        if not self:
            return

        _logger.info(reason, *args)
        self.batch_ids.unlink()
        self.unlink()

    def fail(self, message, prs=None):
        _logger.error("Staging %s failed: %s", self, message)
        prs = prs or self.batch_ids.prs
        prs.write({'state': 'error'})
        for pr in prs:
            pr.repository.github().comment(
                pr.number, "Staging failed: %s" % message)

        self.batch_ids.unlink()
        self.unlink()

    def try_splitting(self):
        batches = len(self.batch_ids)
        if batches > 1:
            midpoint = batches // 2
            h, t = self.batch_ids[:midpoint], self.batch_ids[midpoint:]
            self.env['runbot_merge.stagings'].create({
                'target': self.target.id,
                'batch_ids': [(4, batch.id, 0) for batch in h],
            })
            self.env['runbot_merge.stagings'].create({
                'target': self.target.id,
                'batch_ids': [(4, batch.id, 0) for batch in t],
            })
            # apoptosis
            self.unlink()
            return True

        # single batch => the staging is an unredeemable failure
        if self.state != 'failure':
            # timed out, just mark all PRs (wheee)
            self.fail('timed out (>{} minutes)'.format(self.target.project_id.ci_timeout))
            return False

        # try inferring which PR failed and only mark that one
        for repo, head in json.loads(self.heads).items():
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
                return False

        # the staging failed but we don't have a specific culprit, fail
        # everything
        self.fail("unknown reason")

        return False

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

    prs = fields.One2many('runbot_merge.pull_requests', 'batch_id')

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
            msg = pr.message
            author=None
            if pr.squash:
                # FIXME: maybe should be message of the *first* commit of the branch?
                # TODO: or depend on # of commits in PR instead of squash flag?
                commit = gh.commit(pr.head)
                msg = commit['message']
                author = commit['author']

            msg += '\n\ncloses {pr.repository.name}#{pr.number}'.format(pr=pr)

            try:
                new_heads[pr] = gh.merge(pr.head, 'tmp.{}'.format(pr.target.name), msg, squash=pr.squash, author=author)['sha']
            except exceptions.MergeError:
                _logger.exception("Failed to merge %s:%s into staging branch", pr.repository.name, pr.number)
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
            if not self.env['runbot_merge.commit'].search([('sha', '=', head)]):
                self.env['runbot_merge.commit'].create({'sha': head})
        return self.create({
            'target': prs[0].target.id,
            'prs': [(4, pr.id, 0) for pr in prs],
        })
