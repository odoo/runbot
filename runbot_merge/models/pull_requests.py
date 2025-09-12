from __future__ import annotations

import ast
import builtins
import collections
import contextlib
import datetime
import itertools
import json
import logging
import re
import time
from enum import IntEnum
from functools import reduce
from operator import itemgetter
from typing import Optional, Union, List, Iterator, Tuple

import psycopg2.errors
import sentry_sdk
import werkzeug
from markupsafe import Markup

from odoo import api, fields, models, tools, Command
from odoo.exceptions import AccessError, UserError
from odoo.fields import Domain
from odoo.tools import html_escape, Reverse, mute_logger
from . import commands
from .utils import enum, readonly, dfm

from .. import github, exceptions, controllers, utils

_logger = logging.getLogger(__name__)
FOOTER = '\nMore info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port\n'


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

    id: int

    sequence = fields.Integer(default=50, group_operator=None)
    name = fields.Char(required=True)
    project_id = fields.Many2one('runbot_merge.project', required=True, index=True)
    status_ids = fields.One2many('runbot_merge.repository.status', 'repo_id', string="Required Statuses")

    group_id = fields.Many2one('res.groups', default=lambda self: self.env.ref('base.group_user'))

    branch_filter = fields.Char(default='[(1, "=", 1)]', help="Filter branches valid for this repository")
    substitutions = fields.Text(
        "label substitutions",
        help="""sed-style substitution patterns applied to the label on input, one per line.

All substitutions are tentatively applied sequentially to the input.
""")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if 'status_ids' in vals:
                continue

            st = vals.pop('required_statuses', 'legal/cla,ci/runbot')
            if st:
                vals['status_ids'] = [(0, 0, {'context': c}) for c in st.split(',')]
        return super().create(vals_list)

    def write(self, vals):
        st = vals.pop('required_statuses', None)
        if st:
            vals['status_ids'] = [(5, 0, {})] + [(0, 0, {'context': c}) for c in st.split(',')]
        return super().write(vals)

    def github(self, token_field='github_token') -> github.GH:
        return github.GH(self.project_id[token_field], self.name)

    def _auto_init(self):
        res = super()._auto_init()
        tools.create_unique_index(
            self.env.cr, 'runbot_merge_unique_repo', self._table, ['name'])
        return res

    def _load_pr(
        self,
        number: int,
        *,
        closing: bool = False,
        squash: bool = False,
        ping: str | None = None,
    ):
        gh = self.github()

        # fetch PR object and handle as *opened*
        issue, pr = gh.pr(number)

        repo_name = pr['base']['repo']['full_name']
        if not self.project_id._has_branch(pr['base']['ref']):
            _logger.info("Tasked with loading %s PR %s#%d for un-managed branch %s:%s, ignoring",
                         pr['state'], repo_name, number, self.name, pr['base']['ref'])
            if not closing:
                self.env.ref('runbot_merge.pr.load.unmanaged')._send(
                    repository=self,
                    pull_request=number,
                    format_args = {
                        'pr': pr,
                        'repository': self,
                    },
                )
            return

        # if the PR is already loaded, force sync a few attributes
        pr_id = self.env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo_name),
            ('number', '=', number),
        ])
        if pr_id:
            if squash:
                pr_id.squash = pr['commits'] == 1
                return

            sync = controllers.handle_pr(self.env, {
                'action': 'synchronize',
                'pull_request': pr,
                'sender': {'login': self.project_id.github_prefix}
            })
            edit = controllers.handle_pr(self.env, {
                'action': 'edited',
                'pull_request': pr,
                'changes': {
                    'base': {'ref': {'from': pr_id.target.name}},
                    'title': {'from': pr_id.message.splitlines()[0]},
                    'body': {'from', ''.join(pr_id.message.splitlines(keepends=True)[2:])},
                },
                'sender': {'login': self.project_id.github_prefix},
            })
            edit2 = ''
            if pr_id.draft != pr['draft']:
                edit2 = controllers.handle_pr(self.env, {
                    'action': 'converted_to_draft' if pr['draft'] else 'ready_for_review',
                    'pull_request': pr,
                    'sender': {'login': self.project_id.github_prefix}
                }) + '. '
            if pr_id.state != 'closed' and pr['state'] == 'closed':
                # don't go through controller because try_closing does weird things
                # for safety / race condition reasons which ends up committing
                # and breaks everything
                pr_id.state = 'closed'
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': pr_id.repository.id,
                'pull_request': number,
                'message': f"{edit}. {edit2}{sync}.",
            })
            return

        # special case for closed PRs, just ignore all events and skip feedback
        if closing:
            self.env['runbot_merge.pull_requests']._from_gh(pr)
            return

        sender = {'login': self.project_id.github_prefix}
        # init the PR to the null commit so we can later synchronise it back
        # back to the "proper" head while resetting reviews
        controllers.handle_pr(self.env, {
            'action': 'opened',
            'pull_request': {
                **pr,
                'head': {**pr['head'], 'sha': '0'*40},
                'state': 'open',
            },
            'sender': sender,
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
                    'sender': sender,
                })
            else:
                controllers.handle_comment(self.env, {
                    'action': 'created',
                    'issue': issue,
                    'comment': item,
                    'repository': {'full_name': self.name},
                    'sender': sender,
                })
        # sync to real head
        controllers.handle_pr(self.env, {
            'action': 'synchronize',
            'pull_request': pr,
            'sender': sender,
        })
        pr_id = self.env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo_name),
            ('number', '=', number),
        ])
        if pr['state'] == 'closed':
            # don't go through controller because try_closing does weird things
            # for safety / race condition reasons which ends up committing
            # and breaks everything
            pr_id.closed = True

        self.env.ref('runbot_merge.pr.load.fetched')._send(
            repository=self,
            pull_request=number,
            format_args={
                'pr': pr_id,
                'ping': ping or pr_id.ping,
            },
        )

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

    id: int

    name = fields.Char(required=True)
    project_id = fields.Many2one('runbot_merge.project', required=True, index=True)

    active_staging_id = fields.Many2one(
        'runbot_merge.stagings', compute='_compute_active_staging', store=True, index=True,
        help="Currently running staging for the branch."
    )
    staging_ids = fields.One2many('runbot_merge.stagings', 'target')
    split_ids = fields.One2many('runbot_merge.split', 'target')

    prs = fields.One2many('runbot_merge.pull_requests', 'target', domain=[
        ('state', '!=', 'closed'),
        ('state', '!=', 'merged'),
    ])

    active = fields.Boolean(default=True)
    sequence = fields.Integer(group_operator=None)

    staging_enabled = fields.Boolean(default=True)

    def _auto_init(self):
        res = super()._auto_init()
        tools.create_unique_index(
            self.env.cr, 'runbot_merge_unique_branch_per_repo',
            self._table, ['name', 'project_id'])
        return res

    @api.depends('name', 'active', 'project_id.name')
    def _compute_display_name(self):
        for b in self:
            b.display_name = f"{b.project_id.name}:{b.name}" + ('' if b.active else ' (inactive)')

    def write(self, vals):
        if vals.get('active') is False and (actives := self.filtered('active')):
            actives.active_staging_id.cancel(
                "Target branch deactivated by %r.",
                self.env.user.login,
            )
            tmpl = self.env.ref('runbot_merge.pr.branch.disabled')
            self.env['runbot_merge.pull_requests.feedback'].create([{
                'repository': pr.repository.id,
                'pull_request': pr.number,
                'message': tmpl._format(pr=pr),
            } for pr in actives.prs])
            self.env.ref('runbot_merge.branch_cleanup')._trigger()

        if (
            (vals.get('staging_enabled') is True and not all(self.mapped('staging_enabled')))
         or (vals.get('active') is True and not all(self.mapped('active')))
        ):
            self.env.ref('runbot_merge.staging_cron')._trigger()

        super().write(vals)
        return True

    @api.depends('staging_ids.active')
    def _compute_active_staging(self):
        for b in self:
            b.active_staging_id = b.with_context(active_test=True).staging_ids


class SplitOffWizard(models.TransientModel):
    _name = "runbot_merge.pull_requests.split_off"
    _description = "wizard to split a PR off of its current batch and into a different one"

    pr_id = fields.Many2one("runbot_merge.pull_requests", required=True)
    new_label = fields.Char(string="New Label")

    def button_apply(self):
        self.pr_id._split_off(self.new_label)
        self.unlink()
        return {'type': 'ir.actions.act_window_close'}


ACL = collections.namedtuple('ACL', 'is_admin is_reviewer is_author')
class PullRequests(models.Model):
    _name = 'runbot_merge.pull_requests'
    _description = "Pull Request"
    _inherit = ['mail.thread']
    _order = 'number desc'
    _rec_name = 'number'

    id: int
    display_name: str

    target = fields.Many2one('runbot_merge.branch', required=True, index=True, tracking=True)
    target_sequence = fields.Integer(related='target.sequence')
    repository = fields.Many2one('runbot_merge.repository', required=True)
    project = fields.Many2one(related='repository.project_id')
    # NB: check that target & repo have same project & provide project related?

    closed = fields.Boolean(default=False, tracking=True)
    error = fields.Boolean(string="in error", default=False, tracking=True)
    skipchecks = fields.Boolean(related='batch_id.skipchecks', inverse='_inverse_skipchecks')
    cancel_staging = fields.Boolean(related='batch_id.cancel_staging')
    merge_date = fields.Datetime(
        related='batch_id.merge_date',
        inverse=readonly,
        readonly=True,
        tracking=True,
        store=True,
    )

    state = fields.Selection([
        ('opened', 'Opened'),
        ('closed', 'Closed'),
        ('validated', 'Validated'),
        ('approved', 'Approved'),
        ('ready', 'Ready'),
        # staged?
        ('merged', 'Merged'),
        ('error', 'Error'),
    ],
        compute='_compute_state',
        inverse=readonly,
        readonly=True,
        store=True,
        index=True,
        tracking=True,
        column_type=enum(_name, 'state'),
    )

    number = fields.Integer(required=True, index=True, group_operator=None)
    author = fields.Many2one('res.partner', index=True)
    head = fields.Char(required=True, tracking=True)
    label = fields.Char(
        required=True, index=True, tracking=True,
        help="Label of the source branch (owner:branchname), used for "
             "cross-repository branch-matching"
    )
    refname = fields.Char(compute='_compute_refname')
    message = fields.Text(required=True)
    message_html = fields.Html(compute='_compute_message_html', sanitize=False)
    draft = fields.Boolean(
        default=False, required=True, tracking=True,
        help="A draft PR can not be merged",
    )
    squash = fields.Boolean(default=False, tracking=True)
    merge_method = fields.Selection([
        ('merge', "merge directly, using the PR as merge commit message"),
        ('rebase-merge', "rebase and merge, using the PR as merge commit message"),
        ('rebase-ff', "rebase and fast-forward"),
        ('squash', "squash"),
    ], default=False, tracking=True, column_type=enum(_name, 'merge_method'))
    method_warned = fields.Boolean(default=False)

    reviewed_by = fields.Many2one('res.partner', index=True, tracking=True)
    delegates = fields.Many2many('res.partner', help="Delegate reviewers, not intrinsically reviewers but can review this PR")
    priority = fields.Selection(related="batch_id.priority", inverse=readonly, readonly=True)

    overrides = fields.Char(required=True, default='{}', tracking=True)
    statuses = fields.Text(help="Copy of the statuses from the HEAD commit, as a Python literal", default="{}")
    statuses_full = fields.Text(
        compute='_compute_statuses',
        help="Compilation of the full status of the PR (commit statuses + overrides), as JSON",
        store=True,
    )
    status = fields.Selection([
        ('pending', 'Pending'),
        ('failure', 'Failure'),
        ('success', 'Success'),
    ], compute='_compute_statuses', store=True, inverse=readonly, readonly=True, column_type=enum(_name, 'status'))
    previous_failure = fields.Char(default='{}')

    batch_id = fields.Many2one('runbot_merge.batch', index=True)
    staging_id = fields.Many2one('runbot_merge.stagings', compute='_compute_staging', inverse=readonly, readonly=True, store=True)
    staging_ids = fields.Many2many('runbot_merge.stagings', string="Stagings", compute='_compute_stagings', inverse=readonly, readonly=True, context={"active_test": False})

    @api.depends(
        'closed',
        'batch_id.batch_staging_ids.runbot_merge_stagings_id.active',
    )
    def _compute_staging(self):
        for p in self:
            if p.closed:
                p.staging_id = False
            else:
                p.staging_id = p.batch_id.staging_ids.filtered('active')

    @api.depends('batch_id.batch_staging_ids.runbot_merge_stagings_id')
    def _compute_stagings(self):
        for p in self:
            p.staging_ids = p.batch_id.staging_ids

    commits_map = fields.Char(help="JSON-encoded mapping of PR commits to actually integrated commits. The integration head (either a merge commit or the PR's topmost) is mapped from the 'empty' pr commit (the key is an empty string, because you can't put a null key in json maps).", default='{}')

    link_warned = fields.Boolean(
        default=False, help="Whether we've already warned that this (ready)"
                            " PR is linked to an other non-ready PR"
    )

    blocked = fields.Char(
        compute='_compute_is_blocked', store=True,
        help="PR is not currently stageable for some reason (mostly an issue if status is ready)"
    )

    url = fields.Char(compute='_compute_url')
    github_url = fields.Char(compute='_compute_url')

    repo_name = fields.Char(related='repository.name')
    message_title = fields.Char(compute='_compute_message_title')

    ping = fields.Char(compute='_compute_ping', recursive=True)

    source_id = fields.Many2one('runbot_merge.pull_requests', index=True, help="the original source of this FP even if parents were detached along the way")
    parent_id = fields.Many2one(
        'runbot_merge.pull_requests', index=True,
        help="a PR with a parent is an automatic forward port",
        tracking=True,
    )
    root_id = fields.Many2one('runbot_merge.pull_requests', compute='_compute_root', recursive=True)
    forwardport_ids = fields.One2many('runbot_merge.pull_requests', 'source_id')
    limit_id = fields.Many2one('runbot_merge.branch', help="Up to which branch should this PR be forward-ported", tracking=True)

    detach_reason = fields.Char()

    _sql_constraints = [(
        'fw_constraint',
        'check(source_id is null or num_nonnulls(parent_id, detach_reason) = 1)',
        "fw PRs must either be attached or have a reason for being detached",
    )]

    @api.depends('label')
    def _compute_refname(self):
        for pr in self:
            pr.refname = pr.label.split(':', 1)[-1]

    @api.depends(
        'author.github_login', 'reviewed_by.github_login',
        'source_id.author.github_login', 'source_id.reviewed_by.github_login',
    )
    def _compute_ping(self):
        for pr in self:
            if source := pr.source_id:
                contacts = source.author | source.reviewed_by | pr.reviewed_by
            else:
                contacts = pr.author | pr.reviewed_by

            s = ' '.join(f'@{p.github_login}' for p in contacts)
            pr.ping = s and (s + ' ')

    @api.depends('repository.name', 'number')
    def _compute_url(self):
        base = werkzeug.urls.url_parse(self.env['ir.config_parameter'].sudo().get_param('web.base.url', 'http://localhost:8069'))
        gh_base = werkzeug.urls.url_parse('https://github.com')
        for pr in self:
            path = f'/{werkzeug.urls.url_quote(pr.repository.name)}/pull/{pr.number}'
            pr.url = str(base.join(path))
            pr.github_url = str(gh_base.join(path))

    @api.depends('parent_id.root_id')
    def _compute_root(self):
        for p in self:
            p.root_id = reduce(lambda _, p: p, self._iter_ancestors())

    @api.depends('message')
    def _compute_message_title(self):
        for pr in self:
            pr.message_title = next(iter(pr.message.splitlines()), '')

    @api.depends("message")
    def _compute_message_html(self):
        for pr in self:
            match pr.message.split('\n\n', 1):
                case [title]:
                    pr.message_html = Markup('<h3>%s<h3>') % title
                case [title, description]:
                    pr.message_html = Markup('<h3>%s</h3>\n%s') % (
                        title,
                        dfm(pr.repository.name, description),
                    )
                case _:
                    pr.message_html = ""

    @api.depends('repository.name', 'number', 'message')
    def _compute_display_name(self):
        name_template = '%(repo_name)s#%(number)d'
        if self.env.context.get('pr_include_title'):
            name_template += ' (%(message_title)s)'

        for p in self:
            p.display_name = name_template % p

    def _inverse_skipchecks(self):
        for p in self:
            p.batch_id.skipchecks = p.skipchecks
            if p.skipchecks:
                p.reviewed_by = self.env.user.partner_id


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
        domain = Domain.OR(bits)
        if args:
            domain = Domain.AND([args, domain])
        return self.search(domain, limit=limit).sudo().mapped(lambda r: (r.id, r.display_name))

    @property
    def _approved(self):
        return self.state in ('approved', 'ready')

    @property
    def _ready(self):
        return (self.squash or self.merge_method) and self._approved and self.status == 'success'

    @property
    def _linked_prs(self):
        return self.batch_id.prs - self

    @property
    def limit_pretty(self):
        if self.limit_id:
            return self.limit_id.name

        branches = self.repository.project_id.branch_ids
        if ((bf := self.repository.branch_filter) or '[]') != '[]':
            branches = branches.filtered_domain(ast.literal_eval(bf))
        return branches[:1].name

    @api.depends(
        'batch_id.prs.draft',
        'batch_id.prs.state',
        'batch_id.skipchecks',
    )
    def _compute_is_blocked(self):
        self.blocked = False
        requirements = (
            lambda p: not p.draft,
            lambda p: p.state == 'ready' \
                  or p.batch_id.skipchecks \
                 and all(pr.state != 'error' for pr in p.batch_id.prs)
        )
        messages = ('is in draft', 'is not ready')
        for pr in self:
            if pr.state in ('merged', 'closed'):
                continue

            blocking, message = next((
                (blocking, message)
                for blocking in pr.batch_id.prs
                for requirement, message in zip(requirements, messages)
                if not requirement(blocking)
            ), (None, None))
            if blocking == pr:
                pr.blocked = message
            elif blocking:
                pr.blocked = f"linked PR {blocking.display_name} {message}"

    def _get_overrides(self) -> dict[str, dict[str, str]]:
        if self.parent_id:
            return self.parent_id._get_overrides() | json.loads(self.overrides)
        if self:
            return json.loads(self.overrides)
        return {}

    def _get_or_schedule(
            self,
            repo_name: str,
            number: int,
            *,
            target: str | None = None,
            closing: bool = False,
            commenter: str | None = None,
    ) -> PullRequests | None:
        repo = self.env['runbot_merge.repository'].search([('name', '=', repo_name)])
        if not repo:
            source = self.env['runbot_merge.events_sources'].search([('repository', '=', repo_name)])
            _logger.warning(
                "Got a PR notification for unknown repository %s (source %s)",
                repo_name, source,
            )
            return

        if target:
            b = self.env['runbot_merge.branch'].with_context(active_test=False).search([
                ('project_id', '=', repo.project_id.id),
                ('name', '=', target),
            ])
            tmpl = None if b.active \
                else 'runbot_merge.handle.branch.inactive' if b\
                else 'runbot_merge.pr.fetch.unmanaged'
        else:
            tmpl = None

        pr = self.search([('repository', '=', repo.id), ('number', '=', number)])
        if pr and not pr.target.active:
            tmpl = 'runbot_merge.handle.branch.inactive'
            target = pr.target.name

        if tmpl and not closing:
            self.env.ref(tmpl)._send(
                repository=repo,
                pull_request=number,
                format_args={'repository': repo_name, 'branch': target, 'number': number},
            )

        if pr:
            return pr

        # if the branch is unknown or inactive, no need to fetch the PR
        if tmpl:
            return

        Fetch = self.env['runbot_merge.fetch_job']
        if Fetch.search([('repository', '=', repo.id), ('number', '=', number)]):
            return
        Fetch.create({
            'repository': repo.id,
            'number': number,
            'closing': closing,
            'commenter': commenter,
        })

    def _iter_ancestors(self) -> Iterator[PullRequests]:
        while self:
            yield self
            self = self.parent_id

    def _iter_descendants(self) -> Iterator[PullRequests]:
        pr = self
        while pr := self.search([('parent_id', '=', pr.id)]):
            yield pr

    def _parse_commands(self, author, comment, login):
        assert self, "parsing commands must be executed in an actual PR"

        (login, name) = (author.github_login, author.display_name) if author else (login, 'not in system')

        commandlines = self.repository.project_id._find_commands(comment['body'] or '')
        if not commandlines:
            _logger.info("found no commands in comment of %s (%s) (%s)", login, name,
                 utils.shorten(comment['body'] or '', 50)
            )
            return 'ok'

        def feedback(message: Optional[str] = None, close: bool = False):
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': self.repository.id,
                'pull_request': self.number,
                'message': message,
                'close': close,
            })

        is_admin, is_reviewer, is_author = self._pr_acl(author)
        source_author = self.source_id._pr_acl(author).is_author
        # nota: 15.0 `has_group` completely doesn't work if the recordset is empty
        super_admin = is_admin and author.user_ids and author.user_ids.has_group('runbot_merge.group_admin')

        help_list: list[type(commands.Command)] = list(filter(None, [
            commands.Help,

            (is_reviewer or (self.source_id and source_author)) and not self.reviewed_by and commands.Approve,
            (is_author or source_author) and self.reviewed_by and commands.Reject,
            (is_author or source_author) and self.error and commands.Retry,

            is_author and not self.source_id and commands.FW,
            is_author and commands.Limit,
            source_author and self.source_id and commands.Close,

            is_reviewer and commands.MergeMethod,
            is_reviewer and commands.Delegate,

            is_admin and commands.Priority,
            super_admin and commands.SkipChecks,
            is_admin and commands.CancelStaging,

            author.override_rights and commands.Override,
            is_author and commands.Check,
        ]))
        def format_help(warn_ignore: bool, address: bool = True) -> str:
            s = [
                'Currently available commands{}:'.format(
                    f" for @{login}" if address else ""
                ),
                '',
                '|command||',
                '|-|-|',
            ]
            for command_type in help_list:
                for cmd, text in command_type.help(is_reviewer):
                    s.append(f"|`{cmd}`|{text}|")

            s.extend(['', 'Note: this help text is dynamic and will change with the state of the PR.'])
            if warn_ignore:
                s.extend(["", "Warning: in invoking help, every other command has been ignored."])
            return "\n".join(s)

        try:
            cmds: List[commands.Command] = [
                ps
                for line in commandlines
                for ps in commands.Parser(line.rstrip())
            ]
        except Exception as e:
            _logger.info(
                "error %s while parsing comment of %s (%s): %s",
                e,
                login, name,
                utils.shorten(comment['body'] or '', 50),
            )
            feedback(message=f"""@{login} {e.args[0]}.

For your own safety I've ignored *everything in your entire comment*.

{format_help(False, address=False)}
""")
            return 'error'

        if any(isinstance(cmd, commands.Help) for cmd in cmds):
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': self.repository.id,
                'pull_request': self.number,
                'message': format_help(len(cmds) != 1),
            })
            return "help"

        if not (is_author or self.source_id or (any(isinstance(cmd, commands.Override) for cmd in cmds) and author.override_rights)):
            # no point even parsing commands
            _logger.info("ignoring comment of %s (%s): no ACL to %s",
                          login, name, self.display_name)
            self.env.ref('runbot_merge.command.access.no')._send(
                repository=self.repository,
                pull_request=self.number,
                format_args={'user': login, 'pr': self}
            )
            return 'ignored'

        rejections = []
        for command in cmds:
            msg = None
            match command:
                case commands.Approve() if self.draft:
                    msg = "draft PRs can not be approved."
                case commands.Approve() if self.source_id and (source_author or is_reviewer):
                    if selected := [p for p in self._iter_ancestors() if p.number in command]:
                        for pr in selected:
                            # ignore already reviewed PRs, unless it's the one
                            # being r+'d, this means ancestors in error will not
                            # be warned about
                            if pr == self or not pr.reviewed_by:
                                pr._approve(author, login)
                    else:
                        msg = f"tried to approve PRs {command.fmt()} but no such PR is an ancestors of {self.number}"
                case commands.Approve() if is_reviewer:
                    if command.ids is None or command.ids == [self.number]:
                        msg = self._approve(author, login)
                    else:
                        msg = f"tried to approve PRs {command.fmt()} but the current PR is {self.number}"
                case commands.Reject() if is_author or source_author:
                    if self.batch_id.skipchecks or self.reviewed_by:
                        if self.error:
                            self.error = False
                        if self.reviewed_by:
                            self.reviewed_by = False
                        if self.batch_id.skipchecks:
                            self.batch_id.skipchecks = False
                            self.env.ref("runbot_merge.command.unapprove.p0")._send(
                                repository=self.repository,
                                pull_request=self.number,
                                format_args={'user': login, 'pr': self},
                            )
                        if self.source_id.forwardport_ids.filtered(
                            lambda p: p.reviewed_by and p.state not in ('merged', 'closed')
                        ):
                            feedback("Note that only this forward-port has been"
                                     " unapproved, sibling forward ports may"
                                     " have to be unapproved individually.")
                        self.unstage("unreviewed (r-) by %s", login)
                    else:
                        msg = "r- makes no sense in the current PR state."
                case commands.MergeMethod() if is_reviewer:
                    self.merge_method = command.value
                    explanation = next(label for value, label in type(self).merge_method.selection if value == command.value)
                    self.env.ref("runbot_merge.command.method")._send(
                        repository=self.repository,
                        pull_request=self.number,
                        format_args={'new_method': explanation, 'pr': self, 'user': login},
                    )
                    # if the merge method is the only thing preventing (but not
                    # *blocking*) staging, trigger a staging
                    if self.state == 'ready':
                        self.env.ref("runbot_merge.staging_cron")._trigger()
                case commands.Retry() if is_author or source_author:
                    if self.error:
                        self.error = False
                    else:
                        msg = "retry makes no sense when the PR is not in error."
                case commands.Check() if is_author:
                    self.env['runbot_merge.fetch_job'].create({
                        'repository': self.repository.id,
                        'number': self.number,
                    })
                case commands.Delegate(users) if is_reviewer:
                    if not users:
                        delegates = self.author
                    else:
                        delegates = self.env['res.partner']
                        for login in users:
                            delegates |= delegates.search([('github_login', '=', login)]) or delegates.create({
                                'name': login,
                                'github_login': login,
                            })
                    delegates.write({'delegate_reviewer': [(4, self.id, 0)]})
                case commands.Priority() if is_admin:
                    self.batch_id.priority = str(command)
                case commands.SkipChecks() if super_admin:
                    self.batch_id.skipchecks = True
                    self.reviewed_by = author
                    if not (self.squash or self.merge_method):
                        self.env.ref('runbot_merge.check_linked_prs_status')._trigger()

                    for p in self.batch_id.prs - self:
                        if not p.reviewed_by:
                            p.reviewed_by = author
                case commands.CancelStaging() if is_admin:
                    self.batch_id.cancel_staging = True
                    if not self.batch_id.blocked:
                        if splits := self.target.split_ids:
                            splits.unlink()
                        self.target.active_staging_id.cancel(
                            "Unstaged by %s on %s",
                            author.github_login, self.display_name,
                        )
                case commands.Override(statuses):
                    for status in statuses:
                        overridable = author.override_rights\
                            .filtered(lambda r: not r.repository_id or (r.repository_id == self.repository))\
                            .mapped('context')
                        if status in overridable:
                            self.overrides = json.dumps({
                                **json.loads(self.overrides),
                                status: {
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
                        else:
                            msg = f"you are not allowed to override {status!r}."
                # FW
                case commands.Close() if source_author:
                    feedback(close=True)
                case commands.FW():
                    message = None
                    match command:
                        case commands.FW.NO if is_author or source_author:
                            message = "Disabled forward-porting."
                        case commands.FW.DEFAULT if is_author or source_author:
                            message = "Waiting for CI to create followup forward-ports."
                        case commands.FW.SKIPCI if is_reviewer:
                            message = "Not waiting for CI to create followup forward-ports."
                        # case commands.FW.SKIPMERGE if is_reviewer:
                        #     message = "Not waiting for merge to create followup forward-ports."
                        case _:
                            msg = f"you don't have the right to {command}."
                    if message:
                        # TODO: feedback?
                        if self.source_id:
                            "if the pr is not a source, ignore (maybe?)"
                        elif not self.merge_date:
                            "if the PR is not merged, it'll be fw'd normally"
                        elif self.batch_id.fw_policy != 'no' or command == commands.FW.NO:
                            "if the policy is not being flipped from no to something else, nothing to do"
                        elif branch_key(self.limit_id) <= branch_key(self.target):
                            "if the limit is lower than current (old style ignore) there's nothing to do"
                        else:
                            message = f"Starting forward-port. {message}"
                            self.env['forwardport.batches'].create({
                                'batch_id': self.batch_id.id,
                                'source': 'merge',
                            })

                        (self.source_id or self).batch_id.fw_policy = command.name.lower()
                        feedback(message=message)

                case commands.Limit(branch) if is_author:
                    if branch is None:
                        feedback(message="'ignore' is deprecated, use 'fw=no' to disable forward porting.")
                    limit = branch or self.target.name
                    for p in self.batch_id.prs:
                        ping, m = p._maybe_update_limit(limit)

                        if ping is Ping.ERROR and p == self:
                            msg = m
                        else:
                            if ping:
                                m = f"@{login} {m}"
                            self.env['runbot_merge.pull_requests.feedback'].create({
                                'repository': p.repository.id,
                                'pull_request': p.number,
                                'message': m,
                            })
                case commands.Limit():
                    msg = "you can't set a forward-port limit."
                # NO!
                case _:
                    msg = f"you can't {command}."
            if msg is not None:
                rejections.append(msg)

        cmdstr = ', '.join(map(str, cmds))
        if not rejections:
            _logger.info("%s (%s) applied %s", login, name, cmdstr)
            self._track_set_author(author, fallback=True)
            return 'applied ' + cmdstr

        self.env.cr.rollback()
        rejections_list = ''.join(f'\n- {r}' for r in rejections)
        _logger.info("%s (%s) tried to apply %s%s", login, name, cmdstr, rejections_list)
        footer = '' if len(cmds) == len(rejections) else "\n\nFor your own safety I've ignored everything in your comment."
        if rejections_list:
            rejections = ' ' + rejections_list.removeprefix("\n- ") if rejections_list.count('\n- ') == 1 else rejections_list
            feedback(message=f"@{login}{rejections}{footer}")
        return 'rejected'

    def _maybe_update_limit(self, limit: str) -> Tuple[Ping, str]:
        limit_id = self.env['runbot_merge.branch'].with_context(active_test=False).search([
            ('project_id', '=', self.repository.project_id.id),
            ('name', '=', limit),
        ])
        if not limit_id:
            return Ping.ERROR, f"there is no branch {limit!r}, it can't be used as a forward port target."

        if limit_id != self.target and not limit_id.active:
            return Ping.ERROR, f"branch {limit_id.name!r} is disabled, it can't be used as a forward port target."

        # not forward ported yet, just acknowledge the request
        if not self.source_id and self.state != 'merged':
            self.limit_id = limit_id
            if branch_key(limit_id) <= branch_key(self.target):
                return Ping.NO, "Forward-port disabled (via limit)."
            else:
                suffix = ''
                if self.batch_id.fw_policy == 'no':
                    self.batch_id.fw_policy = 'default'
                    suffix = " Re-enabled forward-porting (you should use "\
                             "`fw=default` to re-enable forward porting "\
                             "after disabling)."
                return Ping.NO, f"Forward-porting to {limit_id.name!r}.{suffix}"

        # if the PR has been forwardported
        prs = (self | self.forwardport_ids | self.source_id | self.source_id.forwardport_ids)
        tip = max(prs, key=pr_key)
        # if the fp tip was closed it's fine
        if tip.state == 'closed':
            return Ping.ERROR, f"{tip.display_name} is closed, no forward porting is going on"

        prs.limit_id = limit_id

        real_limit = max(limit_id, tip.target, key=branch_key)

        addendum = ''
        # check if tip was queued for forward porting, try to cancel if we're
        # supposed to stop here
        if real_limit == tip.target and (task := self.env['forwardport.batches'].search([('batch_id', '=', tip.batch_id.id)])):
            try:
                with self.env.cr.savepoint():
                    self.env.cr.execute(
                        "SELECT FROM forwardport_batches "
                        "WHERE id = %s FOR UPDATE NOWAIT",
                        [task.id])
            except psycopg2.errors.LockNotAvailable:
                # row locked = port occurring and probably going to succeed,
                # so next(real_limit) likely a done deal already
                return Ping.ERROR, (
                    f"Forward port of {tip.display_name} likely already "
                    f"ongoing, unable to cancel, close next forward port "
                    f"when it completes.")
            else:
                self.env.cr.execute("DELETE FROM forwardport_batches WHERE id = %s", [task.id])

        if real_limit != tip.target:
            # forward porting was previously stopped at tip, and we want it to
            # resume
            if tip.state == 'merged':
                if tip.batch_id.source.fw_policy == 'no':
                    # hack to ping the user but not rollback the transaction
                    return Ping.YES, f"can not forward-port, policy is 'no' on {(tip.source_id or tip).display_name}"
                self.env['forwardport.batches'].create({
                    'batch_id': tip.batch_id.id,
                    'source': 'fp' if tip.parent_id else 'merge',
                })
                resumed = tip
            else:
                resumed = tip.batch_id._schedule_fp_followup()
            if resumed:
                addendum += f', resuming forward-port stopped at {tip.display_name}'

        if real_limit != limit_id:
            addendum += f' (instead of the requested {limit_id.name!r} because {tip.display_name} already exists)'

        # get a "stable" root rather than self's to avoid divertences between
        # PRs across a root divide (where one post-root would point to the root,
        # and one pre-root would point to the source, or a previous root)
        root = tip.root_id
        # reference the root being forward ported unless we are the root
        root_ref = '' if root == self else f' {root.display_name}'
        msg = f"Forward-porting{root_ref} to {real_limit.name!r}{addendum}."
        # send a message to the source & root except for self, if they exist
        root_msg = f'Forward-porting to {real_limit.name!r} (from {self.display_name}).'
        self.env['runbot_merge.pull_requests.feedback'].create([
            {
                'repository': p.repository.id,
                'pull_request': p.number,
                'message': root_msg,
                'token_field': 'fp_github_token',
            }
            # send messages to source and root unless root is self (as it
            # already gets the normal message)
            for p in (self.source_id | root) - self
        ])

        return Ping.NO, msg


    def _find_next_target(self) -> Optional[Branch]:
        """ Finds the branch between target and limit_id which follows
        reference
        """
        root = (self.source_id or self)
        if self.target == root.limit_id:
            return None

        branches = root.target.project_id.with_context(active_test=False)._forward_port_ordered()
        if (branch_filter := self.repository.branch_filter) and branch_filter != '[]':
            branches = branches.filtered_domain(ast.literal_eval(branch_filter))

        branches = list(branches)
        from_ = branches.index(self.target) + 1
        to_ = branches.index(root.limit_id) + 1 if root.limit_id else None

        # return the first active branch in the set
        return next((
            branch
            for branch in branches[from_:to_]
            if branch.active
        ), None)


    def _approve(self, author, login):
        oldstate = self.state
        newstate = RPLUS.get(oldstate)
        if not author.email:
            return "I must know your email before you can review PRs. Please contact an administrator."

        if not newstate:
            # Don't fail the entire command if someone tries to approve an
            # already-approved PR.
            if self.error:
                msg = "This PR is already reviewed, it's in error, you might want to `retry` it instead " \
                      "(if you have already confirmed the error is not legitimate)."
            else:
                msg = "This PR is already reviewed, reviewing it again is useless."
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': self.repository.id,
                'pull_request': self.number,
                'message': msg,
            })
            return None

        self.reviewed_by = author
        _logger.debug(
            "r+ on %s by %s (%s->%s) status=%s message? %s",
            self.display_name, author.github_login,
            oldstate, newstate,
            self.status, self.status == 'failure'
        )
        if self.status == 'failure':
            # the normal infrastructure is for failure and
            # prefixes messages with "I'm sorry"
            self.env.ref("runbot_merge.command.approve.failure")._send(
                repository=self.repository,
                pull_request=self.number,
                format_args={'user': login, 'pr': self},
            )
        if not (self.squash or self.merge_method):
            self.env.ref('runbot_merge.check_linked_prs_status')._trigger()
        return None

    def _pr_acl(self, user) -> ACL:
        if not self:
            return ACL(False, False, False)

        is_admin = self.env['res.partner.review'].search_count([
            ('partner_id', '=', user.id),
            ('repository_id', '=', self.repository.id),
            ('review', '=', True) if self.author != user else ('self_review', '=', True),
        ]) == 1
        if is_admin:
            return ACL(True, True, True)

        # delegate on source = delegate on PR
        if self.source_id and self.source_id in user.delegate_reviewer:
            return ACL(False, True, True)
        # delegate on any ancestors ~ delegate on PR (maybe should be any descendant of source?)
        if any(p in user.delegate_reviewer for p in self._iter_ancestors()):
            return ACL(False, True, True)

        # user is probably always False on a forward port
        return ACL(False, False, self.author == user)

    def _validate(self, statuses):
        # could have two PRs (e.g. one open and one closed) at least
        # temporarily on the same head, or on the same head with different
        # targets
        updateable = self.filtered(lambda p: not p.merge_date)
        updateable.statuses = statuses or '{}'
        for pr in updateable:
            if pr.status == "failure":
                statuses = json.loads(pr.statuses_full)
                for ci in pr.repository.status_ids._for_pr(pr).mapped('context'):
                    status = statuses.get(ci) or {'state': 'pending'}
                    if status['state'] in ('error', 'failure'):
                        pr._notify_ci_new_failure(ci, status)
        self.batch_id._schedule_fp_followup()

    def modified(self, fnames, create=False, before=False):
        """ By default, Odoo can't express recursive *dependencies* which is
        exactly what we need for statuses: they depend on the current PR's
        overrides, and the parent's overrides, and *its* parent's overrides, ...

        One option would be to create a stored computed field which accumulates
        the overrides as *fields* can be recursive, but...
        """
        if 'overrides' in fnames:
            descendants_or_self = self.concat(*self._iter_descendants())
            self.env.add_to_compute(self._fields['status'], descendants_or_self)
            self.env.add_to_compute(self._fields['statuses_full'], descendants_or_self)
            self.env.add_to_compute(self._fields['state'], descendants_or_self)
        super().modified(fnames, create, before)

    @api.depends(
        'statuses', 'overrides', 'target', 'parent_id', 'skipchecks',
        'repository.status_ids.context',
        'repository.status_ids.branch_filter',
        'repository.status_ids.prs',
    )
    def _compute_statuses(self):
        for pr in self:
            statuses = {**json.loads(pr.statuses), **pr._get_overrides()}

            pr.statuses_full = json.dumps(statuses, indent=4)
            if pr.skipchecks:
                pr.status = 'success'
                continue

            st = 'success'
            for ci in pr.repository.status_ids._for_pr(pr):
                v = (statuses.get(ci.context) or {'state': 'pending'})['state']
                if v in ('error', 'failure'):
                    st = 'failure'
                    break
                if v == 'pending':
                    st = 'pending'
            if pr.status != 'failure' and st == 'failure':
                pr.unstage("had CI failure after staging")

            pr.status = st

    @api.depends(
        "status", "reviewed_by", "closed", "error" ,
        "batch_id.merge_date",
        "batch_id.skipchecks",
    )
    def _compute_state(self):
        for pr in self:
            if pr.closed:
                pr.state = "closed"
            elif pr.batch_id.merge_date:
                pr.state = 'merged'
            elif pr.error:
                pr.state = "error"
            elif pr.batch_id.skipchecks: # skipchecks behaves as both approval and status override
                pr.state = "ready"
            else:
                states = ("opened", "approved", "validated", "ready")
                pr.state = states[bool(pr.reviewed_by) | ((pr.status == "success") << 1)]


    def _notify_ci_new_failure(self, ci, st):
        prev = json.loads(self.previous_failure)
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
            self.env.ref("runbot_merge.failure.approved")._send(
                repository=self.repository,
                pull_request=self.number,
                format_args={'pr': self, 'status': ci}
            )
        elif self.state == 'opened' and self.parent_id:
            # only care about FP PRs which are not approved / staged / merged yet
            self.env.ref('runbot_merge.forwardport.ci.failed')._send(
                repository=self.repository,
                pull_request=self.number,
                token_field='fp_github_token',
                format_args={'pr': self, 'ci': ci},
            )

    def _auto_init(self):
        for field in self._fields.values():
            if not isinstance(field, fields.Selection) or field.column_type[0] == 'varchar':
                continue

            t = field.column_type[1]
            self.env.cr.execute("SELECT 1 FROM pg_type WHERE typname = %s", [t])
            if not self.env.cr.rowcount:
                self.env.cr.execute(
                    f"CREATE TYPE {t} AS ENUM %s",
                    [tuple(s for s, _ in field.selection)]
                )

        super()._auto_init()
        # incorrect index: unique(number, target, repository).
        tools.drop_index(self.env.cr, 'runbot_merge_unique_pr_per_target', self._table)
        # correct index:
        tools.create_unique_index(
            self.env.cr, 'runbot_merge_unique_pr_per_repo', self._table, ['repository', 'number'])
        self.env.cr.execute("CREATE INDEX IF NOT EXISTS runbot_merge_pr_head "
                         "ON runbot_merge_pull_requests "
                         "USING hash (head)")

    def _get_batch(self, *, target, label):
        batch = self.env['runbot_merge.batch']
        if not re.search(r':patch-\d+$', label):
            batch = batch.search([
                ('merge_date', '=', False),
                ('prs.target', '=', target),
                ('prs.label', '=', label),
            ])
        return batch or batch.create({})

    @api.model_create_multi
    def create(self, vals_list):
        batches = {}
        for vals in vals_list:
            batch_key = vals['target'], vals['label']
            batch = batches.get(batch_key)
            if batch is None:
                batch = batches[batch_key] = self._get_batch(target=vals['target'], label=vals['label'])
            vals['batch_id'] = batch.id

            if 'limit_id' not in vals:
                limits = {p.limit_id for p in batch.prs}
                if len(limits) == 1:
                    vals['limit_id'] = limits.pop().id
                elif limits:
                    repo = self.env['runbot_merge.repository'].browse(vals['repository'])
                    _logger.warning(
                        "Unable to set limit on %s#%s: found multiple limits in batch (%s)",
                        repo.name, vals['number'],
                        ', '.join(
                            f'{p.display_name} => {p.limit_id.name}'
                            for p in batch.prs
                        )
                    )

        prs = super().create(vals_list)
        for pr in prs:
            c = self.env['runbot_merge.commit'].search([('sha', '=', pr.head)])
            pr._validate(c.statuses)

            if pr.state not in ('closed', 'merged'):
                self.env.ref('runbot_merge.pr.created')._send(
                    repository=pr.repository,
                    pull_request=pr.number,
                    format_args={'pr': pr},
                )
        return prs

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

        return self.env['runbot_merge.pull_requests'].create({
            'closed': description['state'] != 'open',
            'number': description['number'],
            'label': repo._remap_label(description['head']['label']),
            'author': author.id,
            'target': branch.id,
            'repository': repo.id,
            'head': description['head']['sha'],
            'squash': description['commits'] == 1,
            'message': utils.make_message(description),
            'draft': description['draft'],
        })

    def write(self, vals):
        if vals.get('squash'):
            vals['merge_method'] = False

        # when explicitly marking a PR as ready
        if vals.get('state') == 'ready':
            # skip validation
            self.batch_id.skipchecks = True
            # mark current user as reviewer
            vals.setdefault('reviewed_by', self.env.user.partner_id.id)
            for p in self.batch_id.prs - self:
                if not p.reviewed_by:
                    p.reviewed_by = self.env.user.partner_id.id

        for pr in self:
            if (t := vals.get('target')) is not None and pr.target.id != t:
                pr.unstage(
                    "target (base) branch was changed from %r to %r",
                    pr.target.display_name,
                    self.env['runbot_merge.branch'].browse(t).display_name,
                )

            if 'message' in vals:
                merge_method = vals['merge_method'] if 'merge_method' in vals else pr.merge_method
                if merge_method not in (False, 'rebase-ff') and pr.message != vals['message']:
                    pr.unstage("merge message updated")

        match vals.get('closed'):
            case True if not self.closed:
                vals['reviewed_by'] = False
            case False if self.closed and not self.batch_id:
                vals['batch_id'] = self._get_batch(
                    target=vals.get('target') or self.target.id,
                    label=vals.get('label') or self.label,
                )
        w = super().write(vals)

        newhead = vals.get('head')
        if newhead:
            authors = self.env.cr.precommit.data.get(f'mail.tracking.author.{self._name}', {})
            for p in self:
                if not (writer := authors.get(p.id)):
                    writer = self.env.user.partner_id
                p.unstage("updated by %s", writer.github_login or writer.name)
            # this can be batched
            c = self.env['runbot_merge.commit'].search([('sha', '=', newhead)])
            self._validate(c.statuses)
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
        """)
        for [ids] in self.env.cr.fetchall():
            prs = self.browse(ids)
            ready = prs.filtered(lambda p: p.state == 'ready')
            unready = (prs - ready).sorted(key=lambda p: (p.repository.name, p.number))

            for r in ready:
                self.env.ref('runbot_merge.pr.linked.not_ready')._send(
                    repository=r.repository,
                    pull_request=r.number,
                    format_args={
                        'pr': r,
                        'siblings': ', '.join(map('{0.display_name}'.format, unready))
                    },
                )
                r.link_warned = True
                if commit:
                    self.env.cr.commit()

        # send feedback for multi-commit PRs without a merge_method (which
        # we've not warned yet)
        methods = ''.join(
            '* `%s` to %s\n' % pair
            for pair in type(self).merge_method.selection
            if pair[0] != 'squash'
        )
        for r in self.search([
            ('state', 'in', ("approved", "ready")),
            ('staging_id', '=', False),
            ('squash', '=', False),
            ('merge_method', '=', False),
            ('method_warned', '=', False),
        ]):
            self.env.ref('runbot_merge.pr.merge_method')._send(
                repository=r.repository,
                pull_request=r.number,
                format_args={'pr': r, 'methods':methods},
            )
            r.method_warned = True
            if commit:
                self.env.cr.commit()

    def _build_message(self, message: Union['PullRequests', str], related_prs: 'PullRequests' = (), merge: bool = True) -> 'Message':
        # handle co-authored commits (https://help.github.com/articles/creating-a-commit-with-multiple-authors/)
        m = Message.from_message(message)
        if not is_mentioned(message, self):
            if merge:
                m.body += f'\n\ncloses {self.display_name}'
            else:
                m.headers.pop('Part-Of', None)
                m.headers.add('Part-Of', self.display_name)

        for r in related_prs:
            if not is_mentioned(message, r, full_reference=True):
                m.headers.add('Related', r.display_name)

        # ensures all reviewers in the review path are on the PR in order:
        # original reviewer, then last conflict reviewer, then current PR
        reviewers = (self | self.root_id | self.source_id)\
            .mapped('reviewed_by.formatted_email')

        sobs = m.headers.getlist('signed-off-by')
        m.headers.remove('signed-off-by')
        m.headers.extend(
            ('signed-off-by', signer)
            for signer in sobs
            if signer not in reviewers
        )
        m.headers.extend(
            ('signed-off-by', reviewer)
            for reviewer in reversed(reviewers)
        )
        return m

    def unstage(self, reason, *args):
        """ If the PR is staged, cancel the staging. If the PR is split and
        waiting, remove it from the split (possibly delete the split entirely)
        """
        split: Split = self.batch_id.split_id
        if split:
            if split.source_id.likely_false_positive:
                split.source_id.likely_false_positive = False
                split.source_id.message_post(
                    body=f"Assuming failure is a true positive due to {self.display_name} being removed from split.",
                )

            if len(split.batch_ids) == 1:
                # only the batch of this PR -> delete split
                split.unlink()
            else:
                # else remove this batch from the split
                self.batch_id.split_id = False

        self.staging_id.cancel('%s ' + reason, self.display_name, *args)

    def _try_closing(self, by):
        # ignore if the PR is already being updated in a separate transaction
        # (most likely being merged?)
        self.flush_recordset(['state', 'batch_id'])
        self.env.cr.execute('''
        SELECT batch_id FROM runbot_merge_pull_requests
        WHERE id = %s AND state != 'merged' AND state != 'closed'
        FOR UPDATE SKIP LOCKED;
        ''', [self.id])
        if not self.env.cr.rowcount:
            return False

        self.unstage("closed by %s", by)
        self.with_context(forwardport_detach_warn=False).write({
            'closed': True,
            'reviewed_by': False,
            'parent_id': False,
            'detach_reason': f"Closed by {by}",
        })
        self.search([('parent_id', '=', self.id)]).write({
            'parent_id': False,
            'detach_reason': f"{by} closed parent PR {self.display_name}",
        })

        return True

    def _fp_conflict_feedback(self, previous_pr, conflicts):
        (h, out, err, hh) = conflicts.get(previous_pr) or (None, None, None, None)
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
            template = 'runbot_merge.forwardport.failure'
            format_args = {
                'pr': self,
                'commits': lines,
                'stdout': sout,
                'stderr': serr,
                'footer': FOOTER,
            }
        elif any(conflicts.values()):
            template = 'runbot_merge.forwardport.linked'
            format_args = {
                'pr': self,
                'siblings': ', '.join(p.display_name for p in (self.batch_id.prs - self)),
                'footer': FOOTER,
            }
        elif not self._find_next_target():
            ancestors = "".join(
                f"* {p.display_name}\n"
                for p in previous_pr._iter_ancestors()
                if p.parent_id
                if p.state not in ('closed', 'merged')
                if p.target.active
            )
            template = 'runbot_merge.forwardport.final'
            format_args = {
                'pr': self,
                'containing': ' containing:' if ancestors else '.',
                'ancestors': ancestors,
                'footer': FOOTER,
            }
        else:
            template = 'runbot_merge.forwardport.intermediate'
            format_args = {
                'pr': self,
                'footer': FOOTER,
            }
        self.env.ref(template)._send(
            repository=self.repository,
            pull_request=self.number,
            token_field='fp_github_token',
            format_args=format_args,
        )

    def button_split(self):
        if len(self.batch_id.prs) == 1:
            raise UserError("Splitting a batch with a single PR is dumb")

        w = self.env['runbot_merge.pull_requests.split_off'].create({
            'pr_id': self.id,
            'new_label': self.label,
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': w._name,
            'res_id': w.id,
            'target': 'new',
            'views': [(False, 'form')],
        }

    def _split_off(self, new_label):
        # should not be usable to move a PR between batches (maybe later)
        batch = self.env['runbot_merge.batch']
        if not re.search(r':patch-\d+$', new_label):
            if batch.search([
                ('merge_date', '=', False),
                ('prs.label', '=', new_label),
            ]):
                raise UserError("Can not split off to an existing batch")

        self.write({
            'label': new_label,
            'batch_id': batch.create({}).id,
        })


class Ping(IntEnum):
    NO = 0
    YES = 1
    ERROR = 2


# ordering is a bit unintuitive because the lowest sequence (and name)
# is the last link of the fp chain, reasoning is a bit more natural the
# other way around (highest object is the last), especially with Python
# not really having lazy sorts in the stdlib
def branch_key(b: Branch, /, _key=itemgetter('sequence', 'name')):
    return Reverse(_key(b))


def pr_key(p: PullRequests, /):
    return branch_key(p.target)


# state changes on reviews
RPLUS = {
    'opened': 'approved',
    'validated': 'ready',
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
    pull_request = fields.Integer(group_operator=None)

    tags_remove = fields.Char(required=True, default='[]')
    tags_add = fields.Char(required=True, default='[]')

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            if values.pop('state_from', None):
                values['tags_remove'] = ALL_TAGS
            if 'state_to' in values:
                values['tags_add'] = _TAGS[values.pop('state_to')]
            if not isinstance(values.get('tags_remove', ''), str):
                values['tags_remove'] = json.dumps(list(values['tags_remove']))
            if not isinstance(values.get('tags_add', ''), str):
                values['tags_add'] = json.dumps(list(values['tags_add']))
        if any(vals_list):
            self.env.ref('runbot_merge.labels_cron')._trigger()
        return super().create(vals_list)

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
                _logger.info(
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

    repository = fields.Many2one('runbot_merge.repository', required=True, index=True)
    # store the PR number (not id) as we may want to send feedback to PR
    # objects on non-handled branches
    pull_request = fields.Integer(group_operator=None, index=True)
    message = fields.Char()
    close = fields.Boolean()
    token_field = fields.Selection(
        [('github_token', "Mergebot")],
        default='github_token',
        string="Bot User",
        help="Token field (from repo's project) to use to post messages"
    )

    @api.model_create_multi
    def create(self, vals_list):
        # any time a feedback is created, it can be sent
        self.env.ref('runbot_merge.feedback_cron')._trigger()
        return super().create(vals_list)

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
                with contextlib.suppress(json.JSONDecodeError):
                    data = json.loads(message or '')
                    message = data.get('message')

                    if data.get('base'):
                        gh('PATCH', f'pulls/{f.pull_request}', json={'base': data['base']})

                    if f.close:
                        pr_to_notify = self.env['runbot_merge.pull_requests'].search([
                            ('repository', '=', repo.id),
                            ('number', '=', f.pull_request),
                        ])
                        if pr_to_notify:
                            pr_to_notify._notify_merged(gh, data)

                if f.close:
                    gh.close(f.pull_request)

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

class FeedbackTemplate(models.Model):
    _name = 'runbot_merge.pull_requests.feedback.template'
    _description = "str.format templates for feedback messages, no integration," \
                   "but that's their purpose"
    _inherit = ['mail.thread']

    template = fields.Text(tracking=True)
    help = fields.Text(readonly=True)

    def _format(self, **args):
        return self.template.format_map(args)

    def _send(self, *, repository: Repository, pull_request: int, format_args: dict, token_field: Optional[str] = None) -> Optional[Feedback]:
        try:
            feedback = {
                'repository': repository.id,
                'pull_request': pull_request,
                'message': self.template.format_map(format_args),
            }
            if token_field:
                feedback['token_field'] = token_field
            return self.env['runbot_merge.pull_requests.feedback'].create(feedback)
        except Exception:
            _logger.exception("Failed to render template %s", self.get_external_id())
            raise


class StagingCommits(models.Model):
    _name = 'runbot_merge.stagings.commits'
    _description = "Mergeable commits for stagings, always the actually merged " \
                   "commit, never a uniquifier"
    _log_access = False

    staging_id = fields.Many2one('runbot_merge.stagings', required=True)
    commit_id = fields.Many2one('runbot_merge.commit', index=True, required=True)
    repository_id = fields.Many2one('runbot_merge.repository', required=True)

    def _auto_init(self):
        super()._auto_init()
        # the same commit can be both head and tip (?)
        tools.create_unique_index(
            self.env.cr, self._table + "_unique",
            self._table, ['staging_id', 'commit_id']
        )
        # there should be one head per staging per repository, unless one is a
        # real head and one is a uniquifier head
        tools.create_unique_index(
            self.env.cr, self._table + "_unique_per_repo",
            self._table, ['staging_id', 'repository_id'],
        )


class StagingHeads(models.Model):
    _name = 'runbot_merge.stagings.heads'
    _description = "Staging heads, may be the staging's commit or may be a " \
                   "uniquifier (discarded on success)"
    _log_access = False

    staging_id = fields.Many2one('runbot_merge.stagings', required=True)
    commit_id = fields.Many2one('runbot_merge.commit', index=True, required=True)
    repository_id = fields.Many2one('runbot_merge.repository', required=True)

    def _auto_init(self):
        super()._auto_init()
        # the same commit can be both head and tip (?)
        tools.create_unique_index(
            self.env.cr, self._table + "_unique",
            self._table, ['staging_id', 'commit_id']
        )
        # there should be one head per staging per repository, unless one is a
        # real head and one is a uniquifier head
        tools.create_unique_index(
            self.env.cr, self._table + "_unique_per_repo",
            self._table, ['staging_id', 'repository_id'],
        )


class Commit(models.Model):
    """Represents a commit onto which statuses might be posted,
    independent of everything else as commits can be created by
    statuses only, by PR pushes, by branch updates, ...
    """
    _name = _description = 'runbot_merge.commit'
    _rec_name = 'sha'

    sha = fields.Char(required=True)
    statuses = fields.Char(help="json-encoded mapping of status contexts to states", default="{}")
    to_check = fields.Boolean(default=False)

    head_ids = fields.Many2many('runbot_merge.stagings', relation='runbot_merge_stagings_heads', column2='staging_id', column1='commit_id')
    commit_ids = fields.Many2many('runbot_merge.stagings', relation='runbot_merge_stagings_commits', column2='staging_id', column1='commit_id')
    pull_requests = fields.One2many('runbot_merge.pull_requests', compute='_compute_prs')

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            values['to_check'] = True
        r = super().create(vals_list)
        self.env.ref("runbot_merge.process_updated_commits")._trigger()
        return r

    def write(self, values):
        values.setdefault('to_check', True)
        r = super().write(values)
        if values['to_check']:
            self.env.ref("runbot_merge.process_updated_commits")._trigger()
        return r

    @mute_logger('odoo.sql_db')
    def _notify(self):
        Stagings = self.env['runbot_merge.stagings']
        PRs = self.env['runbot_merge.pull_requests']
        serialization_failures = False
        # chances are low that we'll have more than one commit
        for c in self.search([('to_check', '=', True)]):
            sha = c.sha
            pr = PRs.search([('head', '=', sha)])
            stagings = Stagings.search([
                ('head_ids.sha', '=', sha),
                ('state', '=', 'pending'),
                ('target.project_id.staging_statuses', '=', True),
            ])
            try:
                c.to_check = False
                c.flush_recordset(['to_check'])
                if pr:
                    pr._validate(c.statuses)
                    pr._track_set_log_message(html_escape(f"statuses changed on {sha}"))

                if stagings:
                    stagings._notify(c)
            except psycopg2.errors.SerializationFailure:
                serialization_failures = True
                _logger.info("Failed to apply commit %s: serialization failure", sha)
                self.env.cr.rollback()
            except Exception:
                _logger.exception("Failed to apply commit %s", sha)
                self.env.cr.rollback()
            else:
                self.env.cr.commit()
        if serialization_failures:
            self.env.ref("runbot_merge.process_updated_commits")._trigger()

    _sql_constraints = [
        ('unique_sha', 'unique (sha)', 'no duplicated commit'),
    ]

    def _auto_init(self):
        res = super()._auto_init()
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS runbot_merge_unique_statuses
            ON runbot_merge_commit
            USING hash (sha)
        """)
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS runbot_merge_to_process
            ON runbot_merge_commit ((1)) WHERE to_check
        """)
        return res

    def _compute_prs(self):
        for c in self:
            c.pull_requests = self.env['runbot_merge.pull_requests'].search([
                ('head', '=', c.sha),
            ])


class Stagings(models.Model):
    _name = 'runbot_merge.stagings'
    _description = "A set of batches being tested for integration"
    _inherit = ['mail.thread']

    target = fields.Many2one('runbot_merge.branch', required=True, index=True)
    parent_id = fields.Many2one('runbot_merge.stagings')
    child_ids = fields.One2many('runbot_merge.stagings', 'parent_id')
    likely_false_positive = fields.Boolean(default=False)

    staging_batch_ids = fields.One2many('runbot_merge.staging.batch', 'runbot_merge_stagings_id')
    batch_ids = fields.Many2many(
        'runbot_merge.batch',
        context={'active_test': False},
        compute="_compute_batch_ids",
        search="_search_batch_ids",
    )
    pr_ids = fields.One2many('runbot_merge.pull_requests', compute='_compute_prs')
    state = fields.Selection([
        ('success', 'Success'),
        ('failure', 'Failure'),
        ('pending', 'Pending'),
        ('cancelled', "Cancelled"),
        ('ff_failed', "Fast forward failed")
    ], default='pending', index=True, store=True, compute='_compute_state')
    active = fields.Boolean(default=True)

    staged_at = fields.Datetime(default=fields.Datetime.now, index=True)
    staging_end = fields.Datetime(store=True)
    staging_duration = fields.Float(compute='_compute_duration')
    timeout_limit = fields.Datetime(store=True, compute='_compute_timeout_limit')
    reason = fields.Text("Reason for final state (if any)")

    head_ids = fields.Many2many('runbot_merge.commit', relation='runbot_merge_stagings_heads', column1='staging_id', column2='commit_id')
    heads = fields.One2many('runbot_merge.stagings.heads', 'staging_id')
    commit_ids = fields.Many2many('runbot_merge.commit', relation='runbot_merge_stagings_commits', column1='staging_id', column2='commit_id')
    commits = fields.One2many('runbot_merge.stagings.commits', 'staging_id')

    statuses = fields.Binary(compute='_compute_statuses')
    statuses_cache = fields.Text(default='{}', required=True)

    issues_to_close = fields.Json(default=lambda _: [], help="list of tasks to close if this staging succeeds")

    @api.depends('staged_at', 'staging_end')
    def _compute_duration(self):
        for s in self:
            s.staging_duration = ((s.staging_end or fields.Datetime.now()) - s.staged_at).total_seconds()

    @api.depends('target.name', 'state', 'reason')
    def _compute_display_name(self):
        for staging in self:
            staging.display_name = "%d (%s, %s%s)" % (
                staging.id,
                staging.target.name,
                staging.state,
                (', ' + staging.reason) if staging.reason else '',
            )

    @api.depends('staging_batch_ids.runbot_merge_batch_id')
    def _compute_batch_ids(self):
        for staging in self:
            staging.batch_ids = staging.staging_batch_ids.runbot_merge_batch_id

    def _search_batch_ids(self, operator, value):
        return [('staging_batch_ids.runbot_merge_batch_id', operator, value)]

    @api.depends('heads', 'statuses_cache')
    def _compute_statuses(self):
        """ Fetches statuses associated with the various heads, returned as
        (repo, context, state, url)
        """
        heads = {h.commit_id: h.repository_id for h in self.mapped('heads')}
        all_heads = self.mapped('head_ids')

        for st in self:
            statuses = json.loads(st.statuses_cache)

            commits = st.head_ids.with_prefetch(all_heads._prefetch_ids)
            st.statuses = [
                (
                    heads[commit].name,
                    context,
                    status.get('state') or 'pending',
                    status.get('target_url') or ''
                )
                for commit in commits
                for context, status in statuses.get(commit.sha, {}).items()
            ]

    def write(self, vals):
        if timeout := vals.get('timeout_limit'):
            self.env.ref("runbot_merge.merge_cron")\
                ._trigger(fields.Datetime.to_datetime(timeout))

        if vals.get('active') is False:
            vals['staging_end'] = fields.Datetime.now()
            self.env.ref("runbot_merge.staging_cron")._trigger()

        return super().write(vals)

    # only depend on staged_at as it should not get modified, but we might
    # update the CI timeout after the staging have been created and we
    # *do not* want to update the staging timeouts in that case
    @api.depends('staged_at')
    def _compute_timeout_limit(self):
        timeouts = set()
        for st in self:
            t = st.timeout_limit = st.staged_at + datetime.timedelta(minutes=st.target.project_id.ci_timeout)
            timeouts.add(t)
        if timeouts:
            # we might have very different limits for each staging so need to schedule them all
            self.env.ref("runbot_merge.merge_cron")._trigger_list(timeouts)

    @api.depends('batch_ids.prs')
    def _compute_prs(self):
        for staging in self:
            staging.pr_ids = staging.batch_ids.prs

    def _notify(self, c: Commit) -> None:
        self.env.cr.execute("""
        UPDATE runbot_merge_stagings
        SET statuses_cache = CASE
            WHEN statuses_cache::jsonb->%(sha)s IS NULL
                THEN jsonb_insert(statuses_cache::jsonb, ARRAY[%(sha)s],  %(statuses)s::jsonb)
            ELSE statuses_cache::jsonb || jsonb_build_object(%(sha)s, %(statuses)s::jsonb)
        END::text
        WHERE id = any(%(ids)s)
        """, {'sha': c.sha, 'statuses': c.statuses, 'ids': self.ids})
        self.modified(['statuses_cache'])

    def post_status(self, sha, context, status, *, target_url=None, description=None):
        if not self.env.user.has_group('runbot_merge.status'):
            raise AccessError("You are not allowed to post a status.")

        now = datetime.datetime.now().isoformat(timespec='seconds')
        for s in self:
            if not s.target.project_id.staging_rpc:
                continue

            if not any(c.commit_id.sha == sha for c in s.commits):
                raise ValueError(f"Staging {s.id} does not have the commit {sha}")

            st = json.loads(s.statuses_cache)
            st.setdefault(sha, {})[context] = {
                'state': status,
                'target_url': target_url,
                'description': description,
                'updated_at': now,
            }
            s.statuses_cache = json.dumps(st)

        return True

    @api.depends(
        "statuses_cache",
        "target",
        "heads.commit_id.sha",
        "heads.repository_id.status_ids.branch_filter",
        "heads.repository_id.status_ids.context",
    )
    def _compute_state(self):
        for staging in self:
            if staging.state != 'pending':
                continue

            # maps commits to the statuses they need
            required_statuses = [
                (h.commit_id.sha, h.repository_id.status_ids._for_staging(staging).mapped('context'))
                for h in staging.heads
            ]
            cmap = json.loads(staging.statuses_cache)

            last_pending = ""
            state = 'success'
            for head, reqs in required_statuses:
                statuses = cmap.get(head) or {}
                for status in (statuses.get(n, {}) for n in reqs):
                    v = status.get('state')
                    if state == 'failure' or v in ('error', 'failure'):
                        state = 'failure'
                    elif v is None:
                        state = 'pending'
                    elif v == 'pending':
                        state = 'pending'
                        last_pending = max(last_pending, status.get('updated_at', ''))
                    else:
                        assert v == 'success'

            staging.state = state
            if staging.state != 'pending':
                self.env.ref("runbot_merge.merge_cron")._trigger()

            if last_pending:
                timeout = datetime.datetime.fromisoformat(last_pending) \
                      + datetime.timedelta(minutes=staging.target.project_id.ci_timeout)

                if timeout > staging.timeout_limit:
                    staging.timeout_limit = timeout
                    self.env.ref("runbot_merge.merge_cron")._trigger(timeout)
                    _logger.debug("%s got pending status, bumping timeout to %s", staging, timeout)

    def action_cancel(self):
        w = self.env['runbot_merge.stagings.cancel'].create({
            'staging_id': self.id,
        })
        return {
            'type': 'ir.actions.act_window',
            'target': 'new',
            'name': f'Cancel staging {self.id} ({self.target.name})',
            'view_mode': 'form',
            'res_model': w._name,
            'res_id': w.id,
        }

    def cancel(self, reason, *args):
        self = self.filtered('active')
        if not self:
            return False

        _logger.info("Cancelling staging %s: " + reason, self, *args)
        self.parent_id.likely_false_positive = False
        self.write({
            'active': False,
            'state': 'cancelled',
            'reason': reason % args,
        })
        return True

    def fail(self, message, prs=None):
        _logger.info("Staging %s failed: %s", self, message)
        prs = prs or self.batch_ids.prs
        prs._track_set_log_message(f'staging {self.id} failed: {message}')
        prs.error = True
        for pr in prs:
           self.env.ref('runbot_merge.pr.staging.fail')._send(
               repository=pr.repository,
               pull_request=pr.number,
               format_args={'pr': pr, 'message': message},
           )

        if not self.target.split_ids:
            self.parent_id.likely_false_positive = False
        self.write({
            'active': False,
            'state': 'failure',
            'reason': message,
        })
        return True

    def try_splitting(self):
        batches = len(self.batch_ids)
        if batches > 1:
            midpoint = batches // 2
            h, t = self.batch_ids[:midpoint], self.batch_ids[midpoint:]
            # NB: batches remain attached to their original staging
            sh, st = self.env['runbot_merge.split'].create([{
                'target': self.target.id,
                'source_id': (self.parent_id or self).id,
                'batch_ids': [Command.link(batch.id) for batch in h],
            }, {
                'target': self.target.id,
                'source_id': (self.parent_id or self).id,
                'batch_ids': [Command.link(batch.id) for batch in t],
            }])
            _logger.info("Split %s to %s (%s) and %s (%s)",
                         self, h, sh, t, st)
            self.write({
                'active': False,
                'state': 'failure',
                'reason': self.reason if self.state == 'failure' else 'timed out',
                'likely_false_positive': not self.parent_id,
            })
            return True

        # single batch => the staging is an irredeemable failure
        if self.state != 'failure':
            # timed out, just mark all PRs (wheee)
            self.fail('timed out (>{} minutes)'.format(self.target.project_id.ci_timeout))
            return False

        staging_statuses = json.loads(self.statuses_cache)
        # try inferring which PR failed and only mark that one
        for head in self.heads:
            required_statuses = set(head.repository_id.status_ids._for_staging(self).mapped('context'))

            statuses = staging_statuses.get(head.commit_id.sha, {})
            reason = next((
                ctx for ctx, result in statuses.items()
                if ctx in required_statuses
                if result.get('state') in ('error', 'failure')
            ), None)
            if not reason:
                continue

            pr = next((pr for pr in self.batch_ids.prs if pr.repository == head.repository_id), None)

            status = statuses[reason]
            viewmore = ''
            if status.get('target_url'):
                viewmore = ' (view more at %(target_url)s)' % status
            if pr:
                self.fail("%s%s" % (reason, viewmore), pr)
            else:
                self.fail('%s on %s%s' % (reason, head.commit_id.sha, viewmore))
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
            self.env.cr.execute('''
            SELECT 1 FROM runbot_merge_pull_requests
            WHERE id in %s
            FOR UPDATE
            ''', [tuple(self.mapped('batch_ids.prs.id'))])
            try:
                with sentry_sdk.start_span(description="merge staging") as span:
                    span.set_tag("staging", self.id)
                    span.set_tag("branch", self.target.name)
                    self._safety_dance(gh, self.commits)
            except exceptions.FastForwardError as e:
                logger.warning(
                    "Could not fast-forward successful staging on %s:%s: %s",
                    e.args[0], self.target.name,
                    e,
                )
                self.write({
                    'state': 'ff_failed',
                    'reason': str(e.__cause__ or e.__context__ or e)
                })
            else:
                prs = self.mapped('batch_ids.prs')
                prs._track_set_log_message(f'staging {self.id} succeeded')
                logger.info(
                    "%s FF successful, marking %s as merged",
                    self, prs.mapped('display_name'),
                )
                self.batch_ids.merge_date = fields.Datetime.now()

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
                if self.issues_to_close:
                    self.env['runbot_merge.issues_closer'].create(self.issues_to_close)
                    self.env.ref('runbot_merge.issues_closer_cron')._trigger()
            finally:
                self.write({'active': False})
        elif self.state == 'failure' or self.is_timed_out():
            self.try_splitting()

    def is_timed_out(self):
        return fields.Datetime.from_string(self.timeout_limit) < datetime.datetime.now()

    def _safety_dance(self, gh, staging_commits: StagingCommits):
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
        """
        tmp_target = 'tmp.' + self.target.name
        # first force-push the current targets to all tmps
        for repo_name in staging_commits.mapped('repository_id.name'):
            g = gh[repo_name]
            g.set_ref(tmp_target, g.head(self.target.name))
        # then attempt to FF the tmp to the staging commits
        for c in staging_commits:
            gh[c.repository_id.name].fast_forward(tmp_target, c.commit_id.sha)
        # there is still a race condition here, but it's way
        # lower than "the entire staging duration"...
        for i, c in enumerate(staging_commits):
            for pause in [0.1, 0.3, 0.5, 0.9, 0]: # last one must be 0/falsy of we lose the exception
                try:
                    gh[c.repository_id.name].fast_forward(
                        self.target.name,
                        c.commit_id.sha
                    )
                except exceptions.FastForwardError:
                    if i and pause:
                        time.sleep(pause)
                        continue
                    raise
                else:
                    break

    @api.returns('runbot_merge.stagings')
    def for_heads(self, *heads):
        """Returns the staging(s) with all the specified heads. Heads should
        be unique git oids.
        """
        if not heads:
            return self.browse(())

        joins = ''.join(
            f'\nJOIN runbot_merge_stagings_heads h{i} ON h{i}.staging_id = s.id'
            f'\nJOIN runbot_merge_commit c{i} ON c{i}.id = h{i}.commit_id AND c{i}.sha = %s\n'
            for i in range(len(heads))
        )
        self.env.cr.execute(f"SELECT s.id FROM runbot_merge_stagings s {joins}", heads)
        stagings = self.browse(id for [id] in self.env.cr.fetchall())
        stagings.check_access_rights('read')
        stagings.check_access_rule('read')
        return stagings

    @api.returns('runbot_merge.stagings')
    def for_commits(self, *heads):
        """Returns the staging(s) with all the specified commits (heads which
        have actually been merged). Commits should be unique git oids.
        """
        if not heads:
            return self.browse(())

        joins = ''.join(
            f'\nJOIN runbot_merge_stagings_commits h{i} ON h{i}.staging_id = s.id'
            f'\nJOIN runbot_merge_commit c{i} ON c{i}.id = h{i}.commit_id AND c{i}.sha = %s\n'
            for i in range(len(heads))
        )
        self.env.cr.execute(f"SELECT s.id FROM runbot_merge_stagings s {joins}", heads)
        stagings = self.browse(id for [id] in self.env.cr.fetchall())
        stagings.check_access_rights('read')
        stagings.check_access_rule('read')
        return stagings

class Split(models.Model):
    _name = _description = 'runbot_merge.split'

    target = fields.Many2one('runbot_merge.branch', required=True)
    source_id = fields.Many2one('runbot_merge.stagings', required=True)
    batch_ids = fields.One2many('runbot_merge.batch', 'split_id', context={'active_test': False})

    def unlink(self):
        if not self.env.context.get('staging_split'):
            self.source_id.likely_false_positive = False
        return super().unlink()


class FetchJob(models.Model):
    _name = _description = 'runbot_merge.fetch_job'

    active = fields.Boolean(default=True)
    repository = fields.Many2one('runbot_merge.repository', required=True)
    number = fields.Integer(required=True, group_operator=None)
    closing = fields.Boolean(default=False)
    commits_at = fields.Datetime(index="btree_not_null")
    commenter = fields.Char()

    @api.model_create_multi
    def create(self, vals_list):
        now = fields.Datetime.now()
        self.env.ref('runbot_merge.fetch_prs_cron')._trigger({
            fields.Datetime.to_datetime(
                vs.get('commits_at') or now
            )
            for vs in vals_list
        })
        return super().create(vals_list)

    def _check(self, commit=False):
        """
        :param bool commit: commit after each fetch has been executed
        """
        now = getattr(builtins, 'current_date', None) or fields.Datetime.to_string(datetime.datetime.now())
        while True:
            f = self.search([
                '|', ('commits_at', '=', False), ('commits_at', '<=', now)
            ], limit=1)
            if not f:
                return

            f.active = False
            self.env.cr.execute("SAVEPOINT runbot_merge_before_fetch")
            try:
                f.repository._load_pr(
                    f.number,
                    closing=f.closing,
                    squash=bool(f.commits_at),
                    ping=f.commenter and f'@{f.commenter} ',
                )
            except Exception:
                self.env.cr.execute("ROLLBACK TO SAVEPOINT runbot_merge_before_fetch")
                _logger.exception("Failed to load pr %s, skipping it", f.number)
            finally:
                self.env.cr.execute("RELEASE SAVEPOINT runbot_merge_before_fetch")

            if commit:
                self.env.cr.commit()


from .stagings_create import is_mentioned, Message
