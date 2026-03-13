from __future__ import annotations

import ast
import builtins
import collections
import contextlib
import datetime
import itertools
import json
import logging
import math
import operator
import re
import shutil
import statistics
import subprocess
import tempfile
import time
import typing
from enum import IntEnum
from functools import reduce
from operator import itemgetter
from typing import Optional, Union, List, Iterator, Tuple

import psycopg2.errors
import sentry_sdk
import werkzeug
import werkzeug.urls
from markupsafe import Markup
from requests import HTTPError

from odoo import api, fields, models, tools, Command
from odoo.addons.base.controllers.rpc import OdooMarshaller
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.osv import expression
from odoo.tools import html_escape, Reverse, mute_logger, groupby
from odoo.tools.safe_eval import safe_eval

from .commands import commands, AccessFailure, Rel
from .. import github, exceptions, controllers, utils, git
from .utils import enum, readonly, dfm

Conflict = tuple[str, str, str, list[str]]

_logger = logging.getLogger(__name__)
FOOTER = '\nMore info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port\n'


Status = collections.namedtuple('Status', 'repository context state url')
OdooMarshaller.dispatch[Status] = OdooMarshaller.dump_array


class StatusConfiguration(models.Model):
    _name = 'runbot_merge.repository.status'
    _description = "required statuses on repositories"
    _rec_name = 'context'
    _log_access = False

    context = fields.Char(required=True)
    repo_id = fields.Many2one('runbot_merge.repository', required=True, ondelete='cascade')
    branch_filter = fields.Char(help="branches this status applies to")
    prs = fields.Selection([
        ('required', 'Required'),
        ('optional', 'Optional'),
        ('ignored', 'Ignored'),
    ],
        default='required',
        required=True,
        string="Applies to pull requests",
        column_type=enum(_name, 'prs'),
    )
    stagings = fields.Boolean(string="Applies to stagings", default=True)

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
        return self._for_branch(pr.target).filtered(lambda p: p.prs != 'ignored')
    def _for_staging(self, staging):
        assert staging._name == 'runbot_merge.stagings', \
            f'Expected staging, got {staging}'
        return self._for_branch(staging.target).filtered('stagings')

    @property
    def _default_pr_state(self) -> typing.Literal['pending', 'success']:
        return 'pending' if self.prs == 'required' else 'success'


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
    fp_remote_target = fields.Char(help="where FP branches get pushed")

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
            self._cr, 'runbot_merge_unique_repo', self._table, ['name'])
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
            set_squash = pr['commits'] == 1
            if squash:
                pr_id.squash = set_squash
                return

            sync = controllers.handle_pr(self.env, {
                'action': 'synchronize',
                'pull_request': pr,
                'sender': {'login': self.project_id.github_prefix}
            }).get_data(True)
            edit = controllers.handle_pr(self.env, {
                'action': 'edited',
                'pull_request': pr,
                'changes': {
                    'base': {'ref': {'from': pr_id.target.name}},
                    'title': {'from': pr_id.message.splitlines()[0]},
                    'body': {'from', ''.join(pr_id.message.splitlines(keepends=True)[2:])},
                },
                'sender': {'login': self.project_id.github_prefix},
            }).get_data(True)
            edit2 = ''
            if pr_id.draft != pr['draft']:
                edit2 = controllers.handle_pr(self.env, {
                    'action': 'converted_to_draft' if pr['draft'] else 'ready_for_review',
                    'pull_request': pr,
                    'sender': {'login': self.project_id.github_prefix}
                }).get_data(True) + '. '
            if pr_id.squash != set_squash:
                pr_id.squash = set_squash
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
    latest_stagings = fields.One2many(
        'runbot_merge.stagings',
        compute='_compute_latest_stagings',
            context={'active_test': False},
    )
    split_ids = fields.One2many('runbot_merge.split', 'target')

    prs = fields.One2many('runbot_merge.pull_requests', 'target', domain=[('open', '=', True)])

    active = fields.Boolean(default=True)
    sequence = fields.Integer(group_operator=None)

    staging_enabled = fields.Boolean(default=True)

    depth_first_splits = fields.Boolean(help="Stage splits depth-first")
    first_split = fields.Many2one('runbot_merge.split', compute='_compute_first_split')
    optimistic_staging_threshold = fields.Integer(
        help="How many batches should be ready for the next staging to be created immediately",
    )
    presplit = fields.Boolean(
        help="Pessimistically create splits alongside the staging",
    )
    on_fail = fields.Selection([
        ('nothing', 'Keep staging splits'),
        ('join', "Join splits"),
        ('delete', "Delete splits"),
    ],
        default='nothing',
        required=True,
        help="Action to perform when a specific batch has been identified as cause of failure"
    )

    def _auto_init(self):
        res = super()._auto_init()
        tools.create_unique_index(
            self._cr, 'runbot_merge_unique_branch_per_repo',
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

    @api.depends('staging_ids.staged_at')
    def _compute_latest_stagings(self):
        self.env.cr.execute("""
        SELECT target, array_agg(id ORDER BY staged_at DESC)
        FROM (
            SELECT
                id, target, staged_at,
                row_number() OVER (PARTITION BY target ORDER BY staged_at DESC) as rn
            FROM runbot_merge_stagings
        ) as t
        WHERE rn <= 12
        GROUP BY target
        """)
        branch_to_stagings = dict(self.env.cr.fetchall())
        Stagings = self.env['runbot_merge.stagings'].with_context(active_test=False)
        for branch in self:
            branch.latest_stagings = Stagings.browse(branch_to_stagings.get(branch.id, ()))

    @api.depends('depth_first_splits', 'split_ids.parent_path')
    def _compute_first_split(self):
        for b in self:
            if b.depth_first_splits:
                b.first_split = b.split_ids.sorted('parent_path')[:1]
            else:
                b.first_split = b.split_ids[:1]


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
    open = fields.Boolean(compute='_compute_open', search='_search_open')

    @api.depends('state')
    def _compute_open(self):
        for p in self:
            p.open = p.state in ('opened', 'validated', 'approved', 'ready', 'error')

    def _search_open(self, operator, value):
        match (operator, value):
            case ('=', True) | ('!=', False):
                return [('state', 'in', ('opened', 'validated', 'approved', 'ready', 'error'))]
            case ('=', False) | ('!=', True):
                return [('state', 'in', ('merged', 'closed'))]
            case op:
                raise AssertionError(f"Unsupported predicate {op} for field `open`")

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
    priority = fields.Selection(related="batch_id.priority", tracking=True)

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

    reminder_next = fields.Datetime(
        default=lambda self: self.env.cr.now() + datetime.timedelta(days=7),
        index=True,
    )

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

    fw_reminder_ids = fields.One2many(
        'runbot_merge.pull_requests.fw_reminder',
        'source_id',
    )

    _sql_constraints = [(
        'fw_constraint',
        'check(source_id is null or num_nonnulls(parent_id, detach_reason) = 1)',
        "fw PRs must either be attached or have a reason for being detached",
    )]

    @api.constrains('overrides')
    def _overrides_validity(self):
        for r in self:
            for context, override in json.loads(r.overrides).items():
                if not context:
                    raise ValidationError("Override keys are gh status contexts and can not be empty")

                for k in ('state', 'description'):
                    if not override.get(k):
                        raise ValidationError(f"Override entry {k!r} is required")

    @api.depends('label')
    def _compute_refname(self):
        for pr in self:
            pr.refname = pr.label.split(':', 1)[-1]

    @api.depends(
        'author.github_login', 'reviewed_by.github_login',
        'source_id.author.github_login', 'source_id.reviewed_by.github_login',
    )
    @api.depends_context('suppress_ping')
    def _compute_ping(self):
        if self.env.context.get('suppress_ping'):
            self.ping = ''
            return

        for pr in self:
            if source := pr.source_id:
                contacts = source.author | source.reviewed_by | pr.reviewed_by
            else:
                contacts = pr.author | pr.reviewed_by

            s = ' '.join(f'@{p.github_login}' for p in contacts if p.github_login)
            pr.ping = s and (s + ' ')

    def _suppress_ping(self):
        return self.with_context(suppress_ping=self.source_id.batch_id.fw_policy=='skipmerge')

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
        domain = expression.OR(bits)
        if args:
            domain = expression.AND([args, domain])
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
            if not pr.open:
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
            'commits_at': closing and (datetime.datetime.now() + datetime.timedelta(minutes=1)),
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

        fedback = False
        def feedback(message: Optional[str] = None, close: bool = False):
            nonlocal fedback
            if message:
                fedback = True
            self.env['runbot_merge.pull_requests.feedback'].create({
                'repository': self.repository.id,
                'pull_request': self.number,
                'message': message,
                'close': close,
            })

        rel = Rel(author=author, pr=self)
        acls = self.env['runbot_merge.acls'].search([
            '|', ('partner_id', '=', False), ('partner_id', '=', author.id),
            '|', ('repository_id', '=', False), ('repository_id', '=', self.repository.id),
        ], order='effect').filtered(
            lambda acl: (not acl.predicate) or safe_eval(acl.predicate, {
                'rel': rel,
            })
        )

        def format_help(warn_ignore: bool, address: bool = True) -> str:
            s = [
                'Currently available commands{}:'.format(
                    f" for @{login}" if address else ""
                ),
                '',
                '|command||',
                '|-|-|',
            ]
            s.extend(
                f"|`{cmd}`|{text}|"
                for cmd, text in acls.help()
            )

            s.extend(['', 'Note: this help text is dynamic and will change with the state of the PR.'])
            if warn_ignore:
                s.extend(["", "Warning: in invoking help, every other command has been ignored."])
            return "\n".join(s)

        try:
            cmds: List[commands.Command] = list(acls.commands_check(
                ps
                for line in commandlines
                for ps in commands.Parser(line.rstrip())
            ))
        except AccessFailure as e:
            _logger.info("ignoring comment of %s (%s): no ACL to %s on %s",
                          login, name, e.args[0], self.display_name)
            feedback(message=f"@{login} you can't {e.args[0]}.")
            return 'ignored'
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

        rejections = []
        for command in cmds:
            msg = None
            match command:
                case commands.Approve() if self.draft:
                    msg = "draft PRs can not be approved."
                case commands.Approve() if self.source_id:
                    if selected := [p for p in self._iter_ancestors() if p.number in command]:
                        msgs = []
                        for pr in selected:
                            # ignore already reviewed PRs, unless it's the one
                            # being r+'d, this means ancestors in error will not
                            # be warned about
                            if pr == self or not pr.reviewed_by:
                                if m := pr._approve(author, login):
                                    msgs.append(m)
                        if msgs:
                            msg = "\n".join(msgs)
                    else:
                        msg = f"tried to approve PRs {command.fmt()} but no such PR is an ancestors of {self.number}"
                case commands.Approve():
                    if command.ids is None or command.ids == [self.number]:
                        msg = self._approve(author, login)
                    else:
                        msg = f"tried to approve PRs {command.fmt()} but the current PR is {self.number}"
                case commands.Reject():
                    if self.batch_id.skipchecks or self.reviewed_by:
                        if self.error:
                            self.error = False
                        if self.reviewed_by:
                            self.reviewed_by = False
                        if self.batch_id.skipchecks:
                            self.batch_id.skipchecks = False
                            fedback = True
                            self.env.ref("runbot_merge.command.unapprove.p0")._send(
                                repository=self.repository,
                                pull_request=self.number,
                                format_args={'user': login, 'pr': self},
                            )
                        if self.source_id.forwardport_ids.filtered(lambda p: p.reviewed_by and p.open):
                            feedback("Note that only this forward-port has been"
                                     " unapproved, sibling forward ports may"
                                     " have to be unapproved individually.")
                        self.unstage("unreviewed (r-) by %s", login)
                    else:
                        msg = "r- makes no sense in the current PR state."
                case commands.MergeMethod():
                    self.merge_method = command.value
                    explanation = next(label for value, label in type(self).merge_method.selection if value == command.value)
                    self.env.ref("runbot_merge.command.method")._send(
                        repository=self.repository,
                        pull_request=self.number,
                        format_args={'new_method': explanation, 'pr': self, 'user': login},
                    )
                    fedback = True
                    # if the merge method is the only thing preventing (but not
                    # *blocking*) staging, trigger a staging
                    if self.state == 'ready':
                        self.env.ref("runbot_merge.staging_cron")._trigger()
                case commands.Retry():
                    if self.error:
                        self.error = False
                    else:
                        msg = "retry makes no sense when the PR is not in error."
                case commands.Check():
                    self.env['runbot_merge.fetch_job'].create({
                        'repository': self.repository.id,
                        'number': self.number,
                    })
                    fedback = True
                case commands.Delegate(users):
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
                case commands.Priority():
                    self.batch_id.priority = str(command)
                case commands.SkipChecks():
                    self.batch_id.skipchecks = True
                    self.reviewed_by = author
                    if not (self.squash or self.merge_method):
                        self.env.ref('runbot_merge.check_merge_method')._trigger()

                    for p in self.batch_id.prs - self:
                        if not p.reviewed_by:
                            p.reviewed_by = author
                case commands.CancelStaging():
                    self.batch_id.cancel_staging = True
                    if not self.batch_id.blocked:
                        if splits := self.target.split_ids:
                            splits.unlink()
                        self.target.active_staging_id.cancel(
                            "Unstaged by %s on %s",
                            author.github_login, self.display_name,
                        )
                case commands.Override(statuses):
                    overridable = {acl.arg for acl in acls if acl.command == 'Override'}
                    if '*' in overridable:
                        overridable.discard('*')
                        overridable.update(author.override_rights \
                            .filtered(lambda r: not r.repository_id or (r.repository_id == self.repository)) \
                            .mapped('context'))
                    for status in statuses:
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
                case commands.Close():
                    feedback(close=True)
                case commands.FW():
                    message = None
                    match command:
                        case _ if self.batch_id.fw_policy == 'skipmerge' and command != commands.FW.SKIPMERGE:
                            msg = "a pull request set to skip merge can not be reverted to other fw methods"
                        case commands.FW.NO:
                            message = "Disabled forward-porting."
                        case commands.FW.DEFAULT:
                            message = "Waiting for CI to create followup forward-ports."
                        case commands.FW.SKIPCI:
                            message = "Not waiting for CI to create followup forward-ports."
                        case commands.FW.SKIPMERGE:
                            if self.source_id or self.merge_date:
                                message = "Forcing all forward ports."
                            else:
                                message = "Not waiting for merge to create followup forward-ports."
                    if message:
                        # TODO: feedback?
                        if self.source_id:
                            "if the pr is not a source, ignore (maybe?)"
                        elif not (self.merge_date or command == commands.FW.SKIPMERGE):
                            "if the PR is not merged or bypassing merge, it'll be fw'd normally"
                        elif command != commands.FW.SKIPMERGE and (self.batch_id.fw_policy != 'no' or command == commands.FW.NO):
                            "if the policy is not being flipped from no to something else, nothing to do"
                        elif branch_key(self.limit_id) <= branch_key(self.target):
                            "if the limit is lower than current (old style ignore) there's nothing to do"
                        else:
                            message = f"Starting forward-port. {message}"
                            self.env['forwardport.batches'].create({
                                'batch_id': self.batch_id.id,
                                'source': 'merge',
                            })

                        self.batch_id.genealogy_ids.fw_policy = command.name.lower()
                        feedback(message=message)

                case commands.Limit(branch):
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
                case commands.RemindMe(branch, message):
                    project = self.repository.project_id
                    if (branch_filter := self.repository.branch_filter) and branch_filter != '[]':
                        domain = ast.literal_eval(branch_filter)
                    else:
                        domain = []
                    target = self.env['runbot_merge.branch'].search([
                        ('project_id', '=', project.id),
                        ('name', '=', branch),
                        *domain
                    ])
                    if not target:
                        msg = f"unknown or invalid branch {branch!r}"
                    elif target in (self.source_id or self).forwardport_ids.target:
                        msg = f"pr is already ported to {branch!r}, I can't remind you for that"
                    else:
                        source = self.source_id or self
                        if not author:
                            author = self.env['res.partner'].create({
                                'name': login,
                                'github_login': login,
                            })
                        source.write({
                            'fw_reminder_ids': [(0, 0, {
                                'branch_id': target.id,
                                'partner_id': author.id,
                                'message': message,
                            })]
                        })
                case commands.Reset():
                    self.env['runbot_merge.split'].search([('target', '=', self.target.id)]).unlink()
                    match command:
                        case commands.Reset.SPLITS:
                            continue
                        case commands.Reset.AUTO:
                            # heuristic: a staging which has run for more than
                            # 40% of the average of the last 10 successful
                            # stagings would be a waste of resources to cancel
                            if latest_stagings := self.env['runbot_merge.stagings'].search([
                                ('active', '=', False),
                                ('target', '=', self.target.id),
                                ('state', '=', 'success'),
                            ], order='id desc', limit=10):
                                d = statistics.mean(s.staging_duration for s in latest_stagings)
                                if self.target.active_staging_id.staging_duration > (d * 0.4):
                                    continue
                        case commands.Reset.STAGING:
                            pass
                    self.target.active_staging_id.cancel("on %s by %s", self.display_name, login)
                # NO!
                case _:
                    msg = f"you can't {command}."
            if msg is not None:
                rejections.append(msg)

        cmdstr = ', '.join(map(str, cmds))
        if not rejections:
            _logger.info("%s (%s) applied %s", login, name, cmdstr)
            self._track_set_author(author, fallback=True)
            if not fedback and (url := comment.get('reactions', {}).get('url')):
                self.env['runbot_merge.pull_requests.feedback'].create({
                    'repository': self.repository.id,
                    'pull_request': self.number,
                    'reaction': url.removeprefix(
                        f"https://api.github.com/repos/{self.repository.name}/"
                    ),
                })
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
                        "WHERE id = any(%s) FOR UPDATE NOWAIT",
                        [task.ids])
            except psycopg2.errors.LockNotAvailable:
                # row locked = port occurring and probably going to succeed,
                # so next(real_limit) likely a done deal already
                return Ping.ERROR, (
                    f"Forward port of {tip.display_name} likely already "
                    f"ongoing, unable to cancel, close next forward port "
                    f"when it completes.")
            else:
                self.env.cr.execute("DELETE FROM forwardport_batches WHERE id = any(%s)", [task.ids])

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

        domain = []
        if (branch_filter := self.repository.branch_filter) and branch_filter != '[]':
            domain = ast.literal_eval(branch_filter)
        branches = list(root.target.project_id.with_context(active_test=False)._forward_port_ordered(domain))

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
            self.env.ref('runbot_merge.check_merge_method')._trigger()
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

    applicable_statuses = fields.Many2many(
        'runbot_merge.repository.status',
        store=False,
        search='_search_applicable_statuses',
    )
    def _search_applicable_statuses(self, operator, value):
        return [
            ('merge_date', '=', False), ('closed', '=', False),
            ('repository.status_ids', operator, value),
        ]

    @api.depends(
        'statuses', 'overrides', 'target', 'parent_id', 'skipchecks',
        'applicable_statuses.context',
        'applicable_statuses.branch_filter',
        'applicable_statuses.prs',
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
                v = (statuses.get(ci.context) or {'state': ci._default_pr_state})['state']
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
            'description': f"Merge {self.display_name} into {self.target.name}",
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
            'description': f"Merged {self.display_name} in {self.target.name} at {payload['sha']}"
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
        tools.drop_index(self._cr, 'runbot_merge_unique_pr_per_target', self._table)
        # correct index:
        tools.create_unique_index(
            self._cr, 'runbot_merge_unique_pr_per_repo', self._table, ['repository', 'number'])
        self._cr.execute("CREATE INDEX IF NOT EXISTS runbot_merge_pr_head "
                         "ON runbot_merge_pull_requests "
                         "USING hash (head)")

    def _get_batch(self, *, target: str, label: str, create: bool = True) -> 'Batch':
        batch = self.env['runbot_merge.batch']
        if not re.search(r':patch-\d+$', label):
            batch = batch.search([
                ('merge_date', '=', False),
                ('prs.target', '=', target),
                ('prs.label', '=', label),
            ])
        if create:
            return batch or batch.create({})
        return batch

    @api.model_create_multi
    def create(self, vals_list):
        created = []
        to_create = []
        old = self.browse()
        batches = {}
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
        if not to_create:
            return old

        new = super().create(to_create)
        for pr in new:
            # If an other PR was created off of the same branch and closed, delete the deleter
            for remover in self.env['forwardport.branch_remover'].search([
                ('pr_id.label', '=', pr.label),
            ]):
                if remover.pr_id.head == pr.head:
                    remover.unlink()

            # FIXME: only if commit based?
            c = self.env['runbot_merge.commit'].search([('sha', '=', pr.head)])
            pr._validate(c.statuses)

            if pr.open:
                self.env.ref('runbot_merge.pr.created')._send(
                    repository=pr.repository,
                    pull_request=pr.number,
                    format_args={'pr': pr},
                )
            # added a new PR to an already forward-ported batch: immediately
            # port forward to complete the genealogy
            if not pr.source_id and (child_batch := self.env['runbot_merge.batch'].search([
                ('parent_id', '=', pr.batch_id.id),
            ])):
                # check if there is already a PR in the slot we want, if so just link it
                if adoptee := child_batch.prs.filtered(lambda p: p.repository == pr.repository):
                    adoptee.source_id = pr
                    adoptee.detach_reason = "Adopted (fake source)"
                    adoptee.message_post(body=f"Linked to new PR {pr.display_name} in previous batch")
                else:
                    self.env['forwardport.batches'].create({
                        'batch_id': pr.batch_id.id,
                        'source': 'complete',
                        'pr_id': pr.id,
                    })

        new = iter(new)
        old = iter(old)
        return self.browse(next(new).id if c else next(old).id for c in created)

    def _from_gh(self, description, author=None, branch=None, repo=None, **kwargs):
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
            **kwargs,
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
            if (t := vals.get('target')) and pr.target.id != t:
                pr.unstage(
                    "target (base) branch was changed from %r to %r",
                    pr.target.display_name,
                    self.env['runbot_merge.branch'].browse(t).display_name,
                )
                if (
                    'batch_id' not in vals
                and (other_batch := self._get_batch(target=t, label=vals.get('label') or pr.label, create=False))
                and other_batch != pr.batch_id
                and not any(p.repository == pr.repository for p in other_batch.prs)
                ):
                    assert len(self) == 1, \
                        "unable to migrate multiple PRs to the same batch"
                    vals['batch_id'] = other_batch.id

            if 'message' in vals:
                merge_method = vals['merge_method'] if 'merge_method' in vals else pr.merge_method
                if merge_method not in (False, 'rebase-ff') and pr.message != vals['message']:
                    pr.unstage("merge message updated")

        remover = self.env['forwardport.branch_remover']
        match vals.get('closed'):
            case True if not self.closed:
                vals['reviewed_by'] = False
                remover.create([{'pr_id': self.id}])
            case False if self.closed and not self.batch_id:
                vals['batch_id'] = self._get_batch(
                    target=vals.get('target') or self.target.id,
                    label=vals.get('label') or self.label,
                )
                remover.search([('pr_id', '=', self.id)]).unlink()

        # if the PR's head is updated, detach (should split off the FP lines as this is not the original code)
        # TODO: better way to do this? Especially because we don't want to
        #       recursively create updates
        # also a bit odd to only handle updating 1 head at a time, but then
        # again 2 PRs with same head is weird so...
        newhead = vals.get('head')
        with_parents = {p: p.parent_id for p in self if p.open if p.parent_id}
        if newhead and not self.env.context.get('ignore_head_update') and newhead != self.head:
            vals.setdefault('statuses', '{}')
            vals.setdefault('parent_id', False)
            if with_parents and vals['parent_id'] is False:
                vals['detach_reason'] = f"head updated from {self.head} to {newhead}"
            # if any children, this is an FP PR being updated, enqueue
            # updating children
            if self.search_count([('parent_id', '=', self.id)], limit=1):
                self.env['forwardport.updates'].create({
                    'original_root': self.root_id.id,
                    'new_root': self.id
                })

        if vals.get('parent_id') and 'source_id' not in vals:
            parent = self.browse(vals['parent_id'])
            vals['source_id'] = (parent.source_id or parent).id

        w = super().write(vals)

        if self.env.context.get('forwardport_detach_warn', True):
            for p, parent in with_parents.items():
                if p.parent_id:
                    continue

                forwardportable = (
                    not self.search_count([('parent_id', '=', p.id)], limit=1)
                    and
                    p._find_next_target()
                )
                if forwardportable:
                    self.env.ref('runbot_merge.forwardport.update.detached')._send(
                        repository=p.repository,
                        pull_request=p.number,
                        token_field='fp_github_token',
                        format_args={'pr': p.with_context(
                            suppress_ping=p.source_id.batch_id.fw_policy=='skipmerge',
                        )},
                    )
                if parent.open:
                    self.env.ref('runbot_merge.forwardport.update.parent')._send(
                        repository=parent.repository,
                        pull_request=parent.number,
                        token_field='fp_github_token',
                        format_args={
                            'pr': parent.with_context(
                                suppress_ping=p.source_id.batch_id.fw_policy=='skipmerge',
                            ),
                            'child': p,
                        },
                    )

        newhead = vals.get('head')
        if newhead:
            authors = self.env.cr.precommit.data.get(f'mail.tracking.author.{self._name}', {})
            for p in self:
                if not (writer := authors.get(p.id)):
                    writer = self.env.user.partner_id
                if vals.get('closed') is False:
                    p.unstage("reopened by %s", writer.github_login or writer.name)
                else:
                    p.unstage("updated by %s", writer.github_login or writer.name)
            if self.project.staging_statuses:
                c = self.env['runbot_merge.commit'].search([('sha', '=', newhead)])
                self._validate(c.statuses)
            else:
                for p in self:
                    p._validate(p.statuses)

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

    def _check_merge_method_configuration(self, commit=False):
        # send feedback for multi-commit PRs without a merge_method (which
        # we've not warned yet)
        methods = ''.join(
            f'* `{value}` to {label}\n'
            for value, label in type(self).merge_method.selection
            if value != 'squash'
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
            if split.staging_id.source_id.likely_false_positive:
                split.staging_id.source_id.likely_false_positive = False
                split.staging_id.source_id.message_post(
                    body=f"Assuming failure is a true positive due to {self.display_name} being removed from split.",
                )

            if len(split.batch_ids) == 1:
                # only the batch of this PR -> delete split
                split.unlink()
            else:
                # else remove this batch from the split
                self.batch_id.split_id = False

        self.staging_id.cancel(f'%s {reason}', self.display_name, *args)

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
        self.search([
            ('parent_id', '=', self.id),
            ('open', '=', True),
        ]).write({
            'parent_id': False,
            'detach_reason': f"{by} closed parent PR {self.display_name}",
        })

        return True

    def _fp_conflict_feedback(self, previous_pr, conflicts):
        (h, out, err, hh) = conflicts.get(previous_pr) or (None, None, None, None)
        if h:
            sout = serr = ''
            newline_ellipsis = '\n[...]'
            if out.strip():
                sout = f"\nstdout:\n```\n{utils.shorten(out, 8096, newline_ellipsis)}\n```\n"
            if err.strip():
                serr = f"\nstderr:\n```\n{utils.shorten(err, 8069, newline_ellipsis)}\n```\n"

            lines = ''
            if len(hh) > 1:
                lines = '\n' + ''.join(
                    f'* {sha}{" <- on this commit" if sha == h else ""}\n'
                    for sha in hh
                )
            template = 'runbot_merge.forwardport.failure'
            format_args = {
                'pr': self._suppress_ping(),
                'commits': lines,
                'stdout': sout,
                'stderr': serr,
                'footer': FOOTER,
            }
        elif any(conflicts.values()):
            template = 'runbot_merge.forwardport.linked'
            format_args = {
                'pr': self._suppress_ping(),
                'siblings': ', '.join(p.display_name for p in (self.batch_id.prs - self)),
                'footer': FOOTER,
            }
        elif not self._find_next_target():
            ancestors = "".join(
                f"* {p.display_name}\n"
                for p in previous_pr._iter_ancestors()
                if p.parent_id
                if p.open
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
            self.display_name, root.display_name, target_branch.name,
        )

        try:
            target_head = next(
                c for c in source.fetch_heads(
                    self.repository,
                    f"refs/heads/{target_branch.name}",
                    root.head,
                ) if c != root.head
            )
        except subprocess.CalledProcessError as e:
            raise ForwardPortError(
                f"Git error while forward porting {self.display_name}:\n{e.stdout}\n{e.stderr}"
            ) from None


        logger.info("Fetched head of %s (%s)", root.display_name, root.head)

        try:
            return None, root._cherry_pick(source, target_branch, target_head)
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
                'merge.conflictstyle=diff3'
            ).with_config(stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            tree = conf.with_config(check=False).merge_tree(
                '--merge-base', commits[0]['parents'][0]['sha'],
                target_head,
                root.head,
            ).stdout.decode().splitlines(keepends=False)[0]
            # if there was a single commit, reuse its message when committing
            # the conflict
            if len(commits) == 1:
                msg = root._make_fp_message(commits[0])
            else:
                out = utils.shorten(out, 8*1024, '[...]')
                err = utils.shorten(err, 8*1024, '[...]')
                msg = f"Cherry pick of {h} failed\n\nstdout:\n{out}\nstderr:\n{err}\n"

            # if a file is modified by the original PR and added by the forward
            # port / conflict it's a modify/delete conflict: the file was
            # deleted in the target branch, and the update (modify) in the
            # original PR causes it to be added back
            base = conf.remote_head(root.repository, root.target.name)
            original_modified = set(conf.diff(
                "--diff-filter=M", "--name-only",
                "--merge-base", base,
                root.head,
            ).stdout.decode().splitlines(keepends=False))
            conflict_added = set(conf.diff(
                "--diff-filter=A", "--name-only",
                target_head,
                tree,
            ).stdout.decode().splitlines(keepends=False))
            if modify_delete := (conflict_added & original_modified):
                # rewrite the tree with conflict markers added to modify/deleted blobs
                tree = conf.modify_delete(tree, modify_delete)

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

    def _cherry_pick(self, repo: git.Repo, branch: Branch, head: str) -> str:
        """ Cherrypicks ``self`` into ``branch``

        :return: the HEAD of the forward-port is successful
        :raises CherrypickError: in case of conflict
        """
        # <xxx>.cherrypick.<number>
        logger = _logger.getChild(str(self.id)).getChild('cherrypick')

        commits = self.repository.github('fp_github_token').commits(self.number)
        logger.info(
            "%s: copy %s commits to %s (%s)%s",
            self, len(commits), branch.name, head,
            ''.join(
                '\n- %s: %s' % (c['sha'], c['commit']['message'].splitlines()[0])
                for c in commits
            )
        )

        conf = repo.with_config(
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        with contextlib.ExitStack() as atexit:
            project = self.repository.project_id

            base = conf = conf.with_params('merge.renamelimit=0', 'merge.renames=copies')
            if project.use_mergiraf and shutil.which('mergiraf'):
                attrs = atexit.enter_context(tempfile.NamedTemporaryFile(buffering=0))
                attrs.write(b'* merge=mergiraf\n')

                conf = conf.with_params(
                    'merge.renamelimit=0',
                    'merge.renames=copies',
                    'merge.conflictStyle=diff3',
                    'merge.mergiraf.name=mergiraf',
                    'merge.mergiraf.driver=mergiraf merge --git %O %A %B -s %S -x %X -y %Y -p %P -l %L',
                    f'core.attributesfile={attrs.name}',
                )

            for commit in commits:
                commit_sha = commit['sha']
                # merge-tree is a bit stupid and gets confused when the options
                # follow the parameters
                r = conf.merge_tree('--merge-base', commit['parents'][0]['sha'], head, commit_sha)
                if not r.stdout and conf is not base:
                    _logger.warning(
                        "mergiraf likely crashed cherrypicking %s into %s (at %s), reverting to normal merge",
                        self.display_name,
                        branch.name,
                        commit_sha,
                    )
                    conf = base
                    r = conf.merge_tree('--merge-base', commit['parents'][0]['sha'], head, commit_sha)

                loglevel = logging.INFO
                if r.returncode == 1:  # failure
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
                elif r.returncode == 0:  # success
                    # By default cherry-pick fails if a non-empty commit becomes
                    # empty (--empty=stop), also it fails when cherrypicking already
                    # empty commits which I didn't think we prevented but clearly we
                    # do...?
                    new_tree = r.stdout.decode().splitlines(keepends=False)[0]
                    parent_tree = conf.rev_parse(f'{head}^{{tree}}').stdout.decode().strip()
                    if parent_tree == new_tree:
                        r.returncode = 1
                        r.stdout = f"You are currently cherry-picking commit {commit_sha}.".encode()
                        r.stderr = b"The previous cherry-pick is now empty, possibly due to conflict resolution."
                else:  # inflateInit, other crashes
                    loglevel = logging.WARNING

                stdout = r.stdout.decode()
                stderr = _clean_rename(r.stderr.decode())
                logger.debug("Cherry-picked %s: %s\n%s\n%s", commit_sha, r.returncode, stdout, stderr)
                if r.returncode: # pick failed, bail
                    logger.log(
                        loglevel,
                        "forward-port of %s (%s) failed at %s",
                        self, self.display_name, commit_sha)

                    raise CherrypickError(commit_sha, stdout, stderr, commits)
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

    def _reminder(self):
        emails = collections.defaultdict(self.browse)
        for source, prs in groupby(self.search([
            ('source_id', '!=', False),
            ('blocked', '!=', False),
            ('open', '=', True),
            ('reminder_next', '<', self.env.cr.now()),
        ], order='source_id, id'), lambda p: p.source_id):
            # only remind on the "tip" of every chain of descendants as they
            # will most likely lead to their parent being validated (?)
            for pr in set(prs).difference(p.parent_id for p in prs):
                # reminder every 7 days for the first 4 weeks, then every 4 weeks
                age = pr.reminder_next - pr.create_date
                if age < datetime.timedelta(days=28):
                    pr.reminder_next += datetime.timedelta(days=7)
                else:
                    pr.reminder_next += datetime.timedelta(days=28)

                # after 6 months, start sending emails
                if age > datetime.timedelta(weeks=26):
                    if author := source.author.email:
                        emails[author] = emails[author].union(*prs)
                    if reviewer := source.reviewed_by.email:
                        emails[reviewer] = emails[reviewer].union(*prs)
                self.env.ref('runbot_merge.forwardport.reminder')._send(
                    repository=pr.repository,
                    pull_request=pr.number,
                    token_field='fp_github_token',
                    format_args={'pr': pr, 'source': pr.source_id},
                )

        try:
            self.env['mail.mail'].sudo().create([
                {
                    'email_to': email,
                    'subject': f"You have {len(prs)} outstanding forward ports",
                    'body': Markup(
                        "<p>The following forward-ports are more than 6 months old "
                        "and were either created or approved by you.</p>"
                        "<p>Please process them appropriately (merge or close them)"
                        "at the earliest.</p>"
                        "<ul>{}</ul>"
                    ).format(Markup("").join(
                        Markup('<li><a href="{link}">{name}</a></li>').format(
                            name=pr.display_name,
                            link=pr.github_url,
                        )
                        for pr in prs
                    ))
                }
                for email, prs in emails.items()
            ])
        except Exception:
            _logger.exception("Failed to create mail")

    def prioritized(self):
        return self.sorted(lambda p: (
            *batch_key(p.batch_id),
            p.repository.sequence,
            p.repository.id,
        ))


def required_statuses(staging):
    # map of commit_oid: statuses
    cmap = json.loads(staging.statuses_cache)
    for head in staging.heads:
        statuses = cmap.get(head.commit_id.sha) or {}
        for context in head.repository_id.status_ids._for_staging(staging).mapped('context'):
            yield statuses.get(context, {})


map_author = operator.itemgetter('name', 'email', 'date')
map_committer = operator.itemgetter('name', 'email')

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
    reaction = fields.Char()
    token_field = fields.Selection(
        [('github_token', "Mergebot"), ('fp_github_token', 'Forwardport Bot')],
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

                if f.reaction:
                    gh('POST', f.reaction, json={'content': 'eyes'})
            except Exception as e:
                if isinstance(e, HTTPError) and e.response.status_code == 500:
                    self.env.context.get('deactivate', lambda _: None)(True)
                    self.env['mail.thread'].message_notify(
                        body=f"Feedback cron failed with {str(e)}, cron has beeen disabled.",
                        partner_ids=self.env.ref('runbot_merge.group_admin').users.partner_id.ids,
                    )
                elif isinstance(e, HTTPError) and e.response.status_code == 404 and f.reaction:
                    _logger.info(
                        "Comment not found (%s) when trying to send a reaction to %s#%s (%s)",
                        e, f.repo.name, f.pull_request, f.reaction,
                    )
                    to_remove.append(f.id)
                    continue

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


class FwReminders(models.Model):
    _name = 'runbot_merge.pull_requests.fw_reminder'
    _description = "Forward-port reminders"
    _order = 'id'

    branch_id = fields.Many2one('runbot_merge.branch', required=True, index=True)
    source_id = fields.Many2one('runbot_merge.pull_requests', required=True, index=True)
    partner_id = fields.Many2one('res.partner', required=True)
    message = fields.Char(required=True)


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

    Notes:

    `commits` is a large table and `statuses` is by far its largest
    fraction (currently averaging 850 bytes per, after toast compression),
    but it's difficult to compress short of dropping status metadata
    entirely, see commit message for more.
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

    def _compute_prs(self):
        for c in self:
            c.pull_requests = self.env['runbot_merge.pull_requests'].search([
                ('head', '=', c.sha),
            ])


class Stagings(models.Model):
    _name = 'runbot_merge.stagings'
    _description = "A set of batches being tested for integration"
    _inherit = ['mail.thread']
    _parent_store = True

    id: int
    target = fields.Many2one('runbot_merge.branch', required=True, index=True)
    parent_id = fields.Many2one('runbot_merge.stagings')
    parent_path = fields.Char(index=True, unaccent=False)
    source_id = fields.Many2one('runbot_merge.stagings', compute='_compute_source_id', recursive=True, store=True, index=True)
    child_ids = fields.One2many('runbot_merge.stagings', 'source_id')
    likely_false_positive = fields.Boolean(default=False, tracking=True)

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
    ], default='pending', index=True, store=True, compute='_compute_state', tracking=True)
    active = fields.Boolean(default=True, tracking=True)

    staged_at = fields.Datetime(default=fields.Datetime.now, index=True)
    staging_end = fields.Datetime(store=True)
    staging_duration = fields.Float(compute='_compute_duration')
    timeout_limit = fields.Datetime(store=True, compute='_compute_timeout_limit')
    reason = fields.Text("Reason for final state (if any)", tracking=True)

    head_ids = fields.Many2many('runbot_merge.commit', relation='runbot_merge_stagings_heads', column1='staging_id', column2='commit_id')
    heads = fields.One2many('runbot_merge.stagings.heads', 'staging_id')
    commit_ids = fields.Many2many('runbot_merge.commit', relation='runbot_merge_stagings_commits', column1='staging_id', column2='commit_id')
    commits = fields.One2many('runbot_merge.stagings.commits', 'staging_id')

    statuses = fields.Binary(compute='_compute_statuses')
    statuses_cache = fields.Text(default='{}', required=True)

    issues_to_close = fields.Json(default=lambda _: [], help="list of tasks to close if this staging succeeds")
    snapshot = fields.Json(required=True)

    losses = fields.Json()

    @api.depends('staged_at', 'staging_end')
    def _compute_duration(self):
        for s in self:
            s.staging_duration = ((s.staging_end or fields.Datetime.now()) - s.staged_at).total_seconds()

    @api.depends('target.name', 'state', 'reason')
    def _compute_display_name(self):
        for staging in self:
            reason = (', ' + staging.reason) if staging.reason else ''
            staging.display_name = f"{staging.id} ({staging.target.name}, {staging.state}{reason})"

    @api.depends('staging_batch_ids.runbot_merge_batch_id')
    def _compute_batch_ids(self):
        for staging in self:
            staging.batch_ids = staging.staging_batch_ids.runbot_merge_batch_id

    def _search_batch_ids(self, operator, value):
        return [('staging_batch_ids.runbot_merge_batch_id', operator, value)]

    @api.depends('parent_id.source_id')
    def _compute_source_id(self):
        for s in self:
            s.source_id = s.parent_id.source_id or s.parent_id or s

    @api.depends('heads', 'statuses_cache')
    def _compute_statuses(self):
        """ Fetches statuses associated with the various heads, returned as
        (repo, context, state, url)
        """
        self.env.cr.execute("""
        SELECT st.id, jsonb_agg(jsonb_build_array(r.name, coalesce(st.statuses_cache::json::jsonb->c.sha, '{}')))
        FROM runbot_merge_stagings st
        LEFT JOIN runbot_merge_stagings_heads head ON (head.staging_id = st.id)
        LEFT JOIN runbot_merge_repository r ON (r.id = head.repository_id)
        LEFT JOIN runbot_merge_commit c ON (c.id = head.commit_id)
        WHERE st.id = any(%s)
        GROUP BY st.id
        """, [self.ids])
        sys = dict(self.env.cr.fetchall())
        for st in self:
            st.statuses = [
                Status(
                    repo,
                    context,
                    status.get('state') or 'pending',
                    status.get('target_url') or ''
                )
                for repo, st in sys[st.id]
                for context, status in st.items()
            ]

    @api.model_create_multi
    def create(self, vals_list):
        Batches = self.env['runbot_merge.batch']
        for vals in vals_list:
            vals['snapshot'] = Batches.browse(
                attrs['runbot_merge_batch_id']
                for cmd, _id, attrs in vals.get('staging_batch_ids', [])
                if cmd == 0
            ).read(['name', 'prs'])
        return super().create(vals_list)

    def write(self, vals):
        if timeout := vals.get('timeout_limit'):
            self.env.ref("runbot_merge.merge_cron")\
                ._trigger(fields.Datetime.to_datetime(timeout))

        if vals.get('active') is False:
            vals['staging_end'] = fields.Datetime.now()
            self.env.ref("runbot_merge.staging_cron")._trigger()

            if self.state == 'success' and self.target.project_id.fp_github_token:
                # check all batches to see if they should be forward ported
                for b in self.with_context(active_test=False).batch_ids:
                    if b.fw_policy == 'no':
                        continue
                    # If all PRs of a batch have parents they're part of an FP
                    # sequence and thus handled separately (by all being ready
                    # which should have occurred before staging).
                    if all(p.parent_id for p in b.prs):
                        continue
                    # If the batch has already been forward ported, no need for
                    # forward port it again obviously.
                    if self.env['runbot_merge.batch']\
                            .with_context(active_test=False)\
                            .search_count([('parent_id', '=', b.id)], limit=1):
                        continue
                    self.env['forwardport.batches'].create({
                        'batch_id': b.id,
                        'source': 'merge',
                    })

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

    def retry(self):
        if self.state in ('success', 'pending'):
            raise UserError("Can only retry failed or cancelled stagings")

        snapshot = self.batch_ids.read(['name', 'prs'])
        if snapshot != self.snapshot:
            raise UserError("The staging's batches have changed since, cannot retry")

        if any(b.blocked for b in self.batch_ids):
            raise UserError("Cannot retry a staging with blocked batches")

        self.target.active_staging_id.cancel("Retrying staging %d", self.id)
        self.target.split_ids.unlink()

        st = try_staging(self.target, self.batch_ids)
        if not st:
            raise UserError("Failed to re-create the staging")

        return {
            'type': 'ir.actions.act_window',
            'target': 'new',
            'name': f"Retry of staging {self.id} ({self.target.name})",
            'view_mode': 'form',
            'res_model': st._name,
            'res_id': st.id,
        }

    applicable_statuses = fields.Many2many(
        'runbot_merge.repository.status',
        store=False,
        search='_search_applicable_statuses',
    )
    def _search_applicable_statuses(self, operator, value):
        return [
            ('active', '=', True),
            ('heads.repository_id.status_ids', operator, value),
        ]

    @api.depends(
        "statuses_cache",
        "target",
        "heads.commit_id.sha",
        "applicable_statuses.branch_filter",
        "applicable_statuses.context",
        "applicable_statuses.stagings",
    )
    def _compute_state(self):
        for staging in self:
            if staging.state != 'pending':
                continue


            last_pending = ""
            state = 'success'
            for status in required_statuses(staging):
                match status.get('state'):
                    case None:
                        state = 'pending'
                    case 'pending':
                        state = 'pending'
                        last_pending = max(last_pending, status.get('updated_at', ''))
                    case 'error' | 'failure':
                        state = 'failure'
                        break
                    case v:
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
        self.source_id.likely_false_positive = False
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
            self.source_id.likely_false_positive = False
        self.write({
            'active': False,
            'state': 'failure',
            'reason': message,
        })
        match self.target.on_fail:
            case 'nothing':
                pass
            case 'join' if len(self.target.split_ids) == 1:
                pass
            case 'join':
                batch_ids = self.target.split_ids.batch_ids
                self.target.split_ids.unlink()
                self.env['runbot_merge.split'].create({
                    'target': self.target.id,
                    'staging_id': self.id,
                    'batch_ids': batch_ids.ids,
                    'original_batches': batch_ids.ids,
                })
            case 'delete':
                self.target.split_ids.unlink()
            case f:
                _logger.warning("Unknown fail handler %r", f)
        return True

    def try_splitting(self):
        batches = len(self.batch_ids)
        if batches > 1:
            midpoint = math.ceil(batches / 2)
            h, t = self.batch_ids[:midpoint], self.batch_ids[midpoint:]
            # NB: batches remain attached to their original staging
            sh, st = self.env['runbot_merge.split'].create([{
                'target': self.target.id,
                'staging_id': self.id,
                'batch_ids': [Command.link(batch.id) for batch in h],
                'original_batches': h.ids,
            }, {
                'target': self.target.id,
                'staging_id': self.id,
                'batch_ids': [Command.link(batch.id) for batch in t],
                'original_batches': t.ids,
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
                self.fail(f"{reason}{viewmore}", pr)
            else:
                self.fail(f'{reason} on {head.commit_id.sha}{viewmore}')
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
                    self._safety_dance(gh)
            except exceptions.FastForwardError as e:
                logger.warning(
                    "Could not fast-forward successful staging on %s:%s: %s",
                    e.args[0], self.target.name,
                    e,
                )
                self.write({
                    'state': 'ff_failed',
                    'reason': str(e.__cause__)
                })
            except exceptions.InconsistentIntegration as e:
                merged, failed = e.args
                merged_repos = merged.repository_id
                failed_repos = failed.repository_id

                merged_batches = self.batch_ids.filtered(lambda b: b.prs.repository <= merged_repos)
                skipped_batches = self.batch_ids.filtered(lambda b: b.prs.repository <= failed_repos)
                error_batches = self.batch_ids - (merged_batches | skipped_batches)

                merged_batches.merge_date = fields.Datetime.now()
                self.target.staging_enabled = False
                self.fail(
                    "inconsistent integration - "
                    f"succeeded for {', '.join(r.name for r in merged_repos)} "
                    f"but failed for {', '.join(r.name for r in failed_repos)}.",
                    prs=error_batches.prs,
                )
                self.with_context(mail_post_autofollow=True).message_post(
                    body=f"Staging on {self.target.name} has been disabled pending investigation.",
                    partner_ids=self.env.ref("runbot_merge.group_admin").users.partner_id.ids,
                )
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
            finally:
                self.write({'active': False})
        elif self.state == 'failure' or self.is_timed_out():
            self.try_splitting()

    def is_timed_out(self):
        return fields.Datetime.from_string(self.timeout_limit) < datetime.datetime.now()

    def _safety_dance(self, gh):
        """ Reverting updates doesn't work if the branches are protected
        (because a revert is basically a force push). So we can update
        REPO_A, then fail to update REPO_B for some reason, and we're hosed.

        To try and make this issue less likely, do the safety dance:

        First, perform a dry run using the tmp branches (which can be
        force-pushed and sacrificed), that way if somebody pushed directly
        to REPO_B during the staging we catch it. If we're really unlucky
        they could still push after the dry run but...
        """
        tmp_target = 'tmp.' + self.target.name
        # first force-push the current targets to all tmps
        for repo_name in self.heads.repository_id.mapped('name'):
            g = gh[repo_name]
            g.set_ref(tmp_target, g.head(self.target.name))
        # then attempt to FF the tmp to the staging commits
        for c in self.heads:
            gh[c.repository_id.name].fast_forward(tmp_target, c.commit_id.sha)
        # there is still a race condition here, but it's way
        # lower than "the entire staging duration"...
        # TODO: skip for "nil" staging commits (iso current head) to limit GH
        #       error sources?
        # TODO: maybe ff commits in repository importance order as well?
        for i, c in enumerate(self.commits):
            for pause in [0.1, 0.3, 0.5, 0.9, 0]: # last one must be 0/falsy of we lose the exception
                try:
                    gh[c.repository_id.name].fast_forward(
                        self.target.name,
                        c.commit_id.sha
                    )
                except exceptions.FastForwardError as e:
                    # If this is the first staging commit, nothing has happened
                    # maybe, technically github could have both accepted and
                    # errored if the incident is bad enough) so just signal an
                    # FF error
                    if not i:
                        raise
                    if i and pause:
                        time.sleep(pause)
                        continue
                    raise exceptions.InconsistentIntegration(
                        self.commits[:i],
                        self.commits[i:],
                    ) from e
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
    staging_id = fields.Many2one('runbot_merge.stagings', required=True)
    # not a real parent path but similar: parent_path sort lexicographically
    # because the separator `/` is 0x2F while digits are in the range 0x30
    # to 0x39, inventing a new different schema doesn't seem worth it even
    # if this one has a slightly different purpose
    parent_path = fields.Char(compute='_compute_parent_path', store=True, index=True, unaccent=False)
    batch_ids = fields.One2many('runbot_merge.batch', 'split_id', context={'active_test': False})
    original_batches = fields.Json()

    @api.depends('staging_id.parent_path')
    def _compute_parent_path(self):
        for s in self:
            assert s.id
            s.parent_path = f'{s.staging_id.parent_path}~{s.id}'

    def unlink(self):
        if not self.env.context.get('staging_split'):
            self.staging_id.source_id.likely_false_positive = False
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
            except psycopg2.errors.IntegrityError:
                self.env.cr.execute("ROLLBACK TO SAVEPOINT runbot_merge_before_fetch")
                f.active = True
                f.commits_at = datetime.datetime.now() + datetime.timedelta(minutes=1)
            except Exception:
                self.env.cr.execute("ROLLBACK TO SAVEPOINT runbot_merge_before_fetch")
                _logger.exception("Failed to load pr %s, skipping it", f.number)
            finally:
                self.env.cr.execute("RELEASE SAVEPOINT runbot_merge_before_fetch")

            if commit:
                self.env.cr.commit()


from .stagings_create import is_mentioned, Message, try_staging, batch_key
