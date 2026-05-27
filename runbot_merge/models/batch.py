from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterator, Reversible

import requests
from markupsafe import Markup
from psycopg2 import sql

from odoo import models, fields, api
from .pull_requests import ForwardPortError, PullRequests
from .utils import EnumSelection
from .. import git

_logger = logging.getLogger(__name__)
FOOTER = '\nMore info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port\n'


class StagingBatch(models.Model):
    _name = 'runbot_merge.staging.batch'
    _description = "link between batches and staging in order to maintain an " \
                   "ordering relationship between the batches of a staging"
    _log_access = False
    _order = 'id'

    runbot_merge_batch_id = fields.Many2one('runbot_merge.batch', required=True)
    runbot_merge_stagings_id = fields.Many2one('runbot_merge.stagings', required=True)

    def init(self):
        super().init()

        self.env.cr.execute(sql.SQL("""
        CREATE UNIQUE INDEX IF NOT EXISTS runbot_merge_staging_batch_idx
            ON {table} (runbot_merge_stagings_id, runbot_merge_batch_id);

        CREATE INDEX IF NOT EXISTS runbot_merge_staging_batch_rev
            ON {table} (runbot_merge_batch_id) INCLUDE (runbot_merge_stagings_id);
        """).format(table=sql.Identifier(self._table)))


class Batch(models.Model):
    """ A batch is a "horizontal" grouping of *codependent* PRs: PRs with
    the same label & target but for different repositories. These are
    assumed to be part of the same "change" smeared over multiple
    repositories e.g. change an API in repo1, this breaks use of that API
    in repo2 which now needs to be updated.
    """
    _name = 'runbot_merge.batch'
    _description = "batch of pull request"
    _inherit = ['mail.thread']
    _parent_store = True
    _order = "id desc"

    name = fields.Char(compute="_compute_name", search="_search_name")
    target = fields.Many2one('runbot_merge.branch', store=True, compute='_compute_target')
    batch_staging_ids = fields.One2many('runbot_merge.staging.batch', 'runbot_merge_batch_id')
    staging_ids = fields.Many2many(
        'runbot_merge.stagings',
        compute="_compute_staging_ids",
        context={'active_test': False},
    )
    split_id = fields.Many2one('runbot_merge.split', index=True)

    all_prs = fields.One2many('runbot_merge.pull_requests', 'batch_id')
    prs = fields.One2many('runbot_merge.pull_requests', compute='_compute_open_prs', search='_search_open_prs')
    active = fields.Boolean(compute='_compute_active', store=True, help="closed batches (batches containing only closed PRs)")

    fw_policy = fields.Selection([
        ('no', "Do not port forward"),
        ('default', "Default"),
        ('skipci', "Skip CI"),
        ('skipmerge', "Skip merge"),
    ], required=True, default="default", string="Forward Port Policy", tracking=True, readonly=True)

    merge_date = fields.Datetime(tracking=True)
    # having skipchecks skip both validation *and approval* makes sense because
    # it's batch-wise, having to approve individual PRs is annoying
    skipchecks = fields.Boolean(
        string="Skips Checks",
        default=False, tracking=True,
        help="Forces entire batch to be ready, skips validation and approval",
    )
    cancel_staging = fields.Boolean(
        string="Cancels Stagings",
        default=False, tracking=True,
        help="Cancels current staging on target branch when becoming ready"
    )
    priority = EnumSelection([
        ('nice', "Nice"),
        ('default', "Default"),
        ('priority', "Priority"),
        ('alone', "Alone"),
    ], default='default', group_operator=None, required=True, tracking=True)

    blocked = fields.Char(store=True, compute="_compute_blocked", tracking=True)
    unblocked_at = fields.Datetime(store=True, compute="_compute_blocked", tracking=True)

    # unlike on PRs, this does not get detached... ? (because batches can be
    # partially detached so that's a PR-level concern)
    parent_path = fields.Char(index=True, unaccent=False)
    parent_id = fields.Many2one("runbot_merge.batch")
    source = fields.Many2one("runbot_merge.batch", compute="_compute_source")
    genealogy_ids = fields.Many2many(
        "runbot_merge.batch",
        compute="_compute_genealogy",
        context={"active_test": False},
    )

    @api.depends('batch_staging_ids.runbot_merge_stagings_id')
    def _compute_staging_ids(self):
        for batch in self:
            batch.staging_ids = batch.batch_staging_ids.runbot_merge_stagings_id

    @api.depends('parent_path')
    def _compute_source(self):
        for b in self:
            source_id, _sep, _rest = b.parent_path.partition('/')
            b.source = self.browse(int(source_id))

    def descendants(self, include_self: bool = False) -> Iterator[Batch]:
        # in DB both will prefix-match on the literal prefix then apply a
        # trivial filter (even though the filter is technically unnecessary for
        # the first form), doing it like this means we don't have to `- self`
        # in the ``not include_self`` case
        if include_self:
            pattern = self.parent_path + '%'
        else:
            pattern = self.parent_path + '_%'

        act = self.env.context.get('active_test', True)
        return self\
            .with_context(active_test=False)\
            .search([("parent_path", '=like', pattern)], order="parent_path")\
            .with_context(active_test=act)

    # also depends on all the descendants of the source or sth
    @api.depends('parent_path')
    def _compute_genealogy(self):
        for batch in self:
            sid = next(iter(batch.parent_path.split('/', 1)))
            batch.genealogy_ids = self \
                .with_context(active_test=False)\
                .search([("parent_path", "=like", f"{sid}/%")], order="parent_path")\

    def _auto_init(self):
        super()._auto_init()

        self.env.cr.execute("""
        CREATE INDEX IF NOT EXISTS runbot_merge_batch_ready_idx
        ON runbot_merge_batch (target, priority)
        WHERE blocked IS NULL;

        CREATE INDEX IF NOT EXISTS runbot_merge_batch_parent_id_idx
        ON runbot_merge_batch (parent_id)
        WHERE parent_id IS NOT NULL;
        """)

    @api.depends('all_prs.closed')
    def _compute_active(self):
        for b in self:
            b.active = not all(p.closed for p in b.all_prs)

    @api.depends('all_prs.closed')
    def _compute_open_prs(self):
        for b in self:
            b.prs = b.all_prs.filtered(lambda p: not p.closed)

    def _search_open_prs(self, operator, value):
        return [('all_prs', operator, value), ('active', '=', True)]

    @api.depends("prs.label")
    def _compute_name(self):
        for batch in self:
            batch.name = batch.prs[:1].label or batch.all_prs[:1].label

    def _search_name(self, operator, value):
        return [('all_prs.label', operator, value)]

    @api.depends("all_prs.target", "all_prs.closed")
    def _compute_target(self):
        for batch in self:
            targets = batch.prs.mapped('target') or batch.all_prs.mapped('target')
            batch.target = targets if len(targets) == 1 else False

    @api.depends(
        "merge_date",
        "prs.error", "prs.draft",
        "skipchecks",
        "prs.status", "prs.reviewed_by", "prs.target",
    )
    def _compute_blocked(self):
        Validator = self.env['runbot_merge.batch.validate']
        for batch in self:
            if batch.merge_date:
                batch.blocked = "Merged."
            elif not batch.active:
                batch.blocked = "all prs are closed"
            elif len(targets := batch.prs.mapped('target')) > 1:
                batch.blocked = f"Multiple target branches: {', '.join(targets.mapped('name'))!r}"
            elif blocking := batch.prs.filtered(
                lambda p: p.error or p.draft
            ):
                batch.blocked = "Pull request(s) %s blocked." % ', '.join(blocking.mapped('display_name'))
            elif not batch.skipchecks and (unready := batch.prs.filtered(
                lambda p: not (p.reviewed_by and p.status == "success")
            )):
                unreviewed = ', '.join(unready.filtered(lambda p: not p.reviewed_by).mapped('display_name'))
                unvalidated = ', '.join(unready.filtered(lambda p: p.status == 'pending').mapped('display_name'))
                failed = ', '.join(unready.filtered(lambda p: p.status == 'failure').mapped('display_name'))
                batch.blocked = "Pull request(s) %s." % ', '.join(filter(None, [
                    unreviewed and f"{unreviewed} are waiting for review",
                    unvalidated and f"{unvalidated} are waiting for CI",
                    failed and f"{failed} have failed CI",
                ]))
            else:
                if batch.blocked:
                    batch.unblocked_at = fields.Datetime.now()
                    self.env.ref("runbot_merge.staging_cron")._trigger()
                    if batch.cancel_staging:
                        if splits := batch.target.split_ids:
                            splits.unlink()
                        batch.target.active_staging_id.cancel(
                            'unstaged by %s becoming ready',
                            ', '.join(batch.prs.mapped('display_name')),
                        )
                    else:
                        Validator.create({'batch_id': batch.id})
                batch.blocked = False
                continue
            batch.unblocked_at = False
            Validator.search([('batch_id', '=', batch.id)]).unlink()
        self._schedule_fp_followup()


    def _port_forward(self):
        if not self:
            return

        proj = self.target.project_id
        if not proj.fp_github_token:
            _logger.warning(
                "Can not forward-port %s (%s): no token on project %s",
                self,
                ', '.join(self.prs.mapped('display_name')),
                proj.name
            )
            return

        notarget = [r.name for r in self.prs.repository if not r.fp_remote_target]
        if notarget:
            _logger.error(
                "Can not forward-port %s (%s): repos %s don't have a forward port remote configured",
                self,
                ', '.join(self.prs.mapped('display_name')),
                ', '.join(notarget),
            )
            return

        all_targets = [p._find_next_target() for p in self.prs]

        if all(t is None for t in all_targets):
            # TODO: maybe add a feedback message?
            _logger.info(
                "Will not forward port %s (%s): no next target",
                self,
                ', '.join(self.prs.mapped('display_name'))
            )
            return

        PRs = self.env['runbot_merge.pull_requests']
        targets = defaultdict(lambda: PRs)
        for p, t in zip(self.prs, all_targets):
            if t:
                targets[t] |= p
            else:
                _logger.info("Skip forward porting %s (of %s): no next target", p.display_name, self)


        # all the PRs *with a next target* should have the same, we can have PRs
        # stopping forward port earlier but skipping... probably not
        if len(targets) != 1:
            for t, prs in targets.items():
                linked, other = next((
                    (linked, other)
                    for other, linkeds in targets.items()
                    if other != t
                    for linked in linkeds
                ))
                for pr in prs:
                    self.env.ref('runbot_merge.forwardport.failure.discrepancy')._send(
                        repository=pr.repository,
                        pull_request=pr.number,
                        token_field='fp_github_token',
                        format_args={'pr': pr, 'linked': linked, 'next': t.name, 'other': other.name},
                    )
            _logger.warning(
                "Cancelling forward-port of %s (%s): found different next branches (%s)",
                self,
                ', '.join(self.prs.mapped('display_name')),
                ', '.join(t.name for t in targets),
            )
            return

        target, prs = next(iter(targets.items()))
        # this is run by the cron, no need to check if otherwise scheduled:
        # either the scheduled job is this one, or it's an other scheduling
        # which will run after this one and will see the port already exists
        if self.with_context(active_test=False).search_count([('parent_id', '=', self.id), ('target', '=', target.id)]):
            _logger.warning(
                "Will not forward-port %s (%s): already ported",
                self,
                ', '.join(prs.mapped('display_name'))
            )
            return

        refname = self.genealogy_ids[0].name.split(':', 1)[-1]
        new_branch = f'{target.name}-{refname}-{self.id}-fw'
        _logger.info("Forward-porting %s to %s (using branch %r)", self, target.name, new_branch)

        conflicts = {}
        commits_count = {}
        for pr in prs:
            repo = git.get_local(pr.repository)
            conflicts[pr], head, commits_count[pr] = pr._create_port_branch(repo, target, forward=True)
            r = repo.check(False).push(git.fw_url(pr.repository), f"{head}:refs/heads/{new_branch}")
            if r.returncode:
                self._delete_fw_branches(conflicts.keys(), new_branch)

                raise ForwardPortError(
                    f"Unable to push forward-port branch for {pr.display_name}:\n{r.stderr.decode()}"
                )

        gh = requests.Session()
        gh.headers['Authorization'] = 'token %s' % proj.fp_github_token
        has_conflicts = any(conflicts.values())
        # could create a batch here but then we'd have to update `_from_gh` to
        # take a batch and then `create` to not automatically resolve batches,
        # easier to not do that.
        new_batch = PRs.browse(())
        self.env.cr.execute('LOCK runbot_merge_pull_requests IN SHARE MODE')
        for pr in prs:
            owner, _ = pr.repository.fp_remote_target.split('/', 1)
            source = pr.source_id or pr
            root = pr.root_id

            message = source.message + '\n\n' + '\n'.join(
                "Forward-Port-Of: %s" % p.display_name
                for p in root | source
            )

            title, body = re.fullmatch(r'(?P<title>[^\n]+)\n*(?P<body>.*)', message, flags=re.DOTALL).groups()
            r = gh.post(f'https://api.github.com/repos/{pr.repository.name}/pulls', json={
                'base': target.name,
                'head': f'{owner}:{new_branch}',
                'title': title,
                'body': body
            })
            if not r.ok:
                _logger.warning("Failed to create forward-port PR for %s, deleting branches", pr.display_name)
                # delete all the branches this should automatically close the
                # PRs if we've created any.
                self._delete_fw_branches(prs, new_branch)
                raise ForwardPortError(f"Failed to create forward-port PR for {pr.display_name} ({r.text})")

            report_conflicts = has_conflicts and self.fw_policy != 'skipmerge'
            new_pr = PRs._from_gh(
                r.json(),
                merge_method=pr.merge_method,
                source_id=source.id,
                parent_id=False if report_conflicts else pr.id,
                detach_reason=report_conflicts and "conflicts:\n{}".format(
                    '\n\n'.join(
                        f"{out}\n{err}".strip()
                        for _, out, err, _ in filter(None, conflicts.values())
                    )
                ),
                squash=commits_count[pr]==1,
            )
            _logger.info("Created forward-port PR %s", new_pr)
            new_batch |= new_pr
            if report_conflicts and pr.parent_id and pr.state not in ('merged', 'closed'):
                self.env.ref('runbot_merge.forwardport.failure.conflict')._send(
                    repository=pr.repository,
                    pull_request=pr.number,
                    token_field='fp_github_token',
                    format_args={'source': source, 'pr': pr._suppress_ping(), 'new': new_pr, 'footer': FOOTER},
                )

        for pr, new_pr in zip(prs, new_batch):
            new_pr._fp_conflict_feedback(pr, conflicts)

            if messages := ((pr.source_id or pr).fw_reminder_ids.filtered(lambda r: r.branch_id == target)):
                self.env['runbot_merge.pull_requests.feedback'].create({
                    'repository': pr.repository.id,
                    'pull_request': new_pr.number,
                    'message': "\n".join(
                        f"@{m.partner_id.github_login} {m.message}"
                        for m in messages
                    ),
                })

            labels = ['forwardport']
            if has_conflicts:
                labels.append('conflict')
            self.env['runbot_merge.pull_requests.tagging'].create({
                'repository': new_pr.repository.id,
                'pull_request': new_pr.number,
                'tags_add': labels,
            })

        new_batch = new_batch.batch_id
        new_batch.parent_id = self
        new_batch.fw_policy = self.fw_policy
        if not self.parent_id and proj.fw_nice and self.priority == 'default' :
            new_batch.priority = 'nice'
        else:
            new_batch.priority = self.priority
        # try to schedule followup
        new_batch._schedule_fp_followup()
        return new_batch

    def _delete_fw_branches(self, previous_prs: Reversible[PullRequests], new_branch: str) -> None:
        for pr in reversed(previous_prs):
            r = git.get_local(pr.repository) \
                .with_config(check=False) \
                .push(git.fw_url(pr.repository), f":refs/heads/{new_branch}")
            if r.returncode:
                _logger.warning("Deleting %s:%s=%s", pr.repository.fp_remote_target, new_branch, r.stderr.decode())
                self.with_context(mail_post_autofollow=True).message_post(
                    body=Markup(
                        "<p>Attempt to delete already pushed branches"
                        " after port failure has also failed, this"
                        " batch likely will not forward port without"
                        " manual interventions.</p><pre>{}</pre>",
                    ).format(r.stderr.decode()),
                    partner_ids=self.env.ref("runbot_merge.group_admin").users.partner_id.ids,
                )
            else:
                _logger.info("Deleting %s:%s=success", pr.repository.fp_remote_target, new_branch)

    def _schedule_fp_followup(self, *, force_fw=False):
        _logger = logging.getLogger(__name__).getChild('forwardport.next')
        # if the PR has a parent and is CI-validated, enqueue the next PR
        scheduled = self.browse(())
        for batch in self:
            fw_policy = batch.source.fw_policy
            ident = str(batch) + ', '.join(batch.prs.mapped('display_name'))

            _logger.info('Checking if forward-port %s', ident)
            if fw_policy == 'no':
                _logger.info('-> disabled on %s', ident)
                continue
            # even if we force_fw, a *followup* should still only be for forward
            # ports so check that the batch has a parent
            if not batch.parent_id:
                _logger.info('-> batch has no parent %s', ident)
                continue
            # in case of conflict or update individual PRs will "lose" their
            # parent, which should prevent forward porting
            if not force_fw and fw_policy != 'skipmerge':
                if orphans := batch.prs.filtered(lambda p: not p.parent_id):
                    _logger.info('-> prs without parent %s: %s', batch, orphans.mapped('display_name'))
                    continue
                if fw_policy != 'skipci' and (
                        invalid := batch.prs.filtered(lambda p: p.status != 'success')
                ):
                    _logger.info(
                        '-> wrong state in %s: %s',
                        ident,
                        ', '.join(f"{p.display_name}: {p.state}" for p in invalid),
                    )
                    continue

            # check if we've already forward-ported this branch
            next_target = batch._find_next_targets()
            if not next_target:
                _logger.info("-> forward port done (no next target) for %s", ident)
                continue
            if len(next_target) > 1:
                _logger.error(
                    "-> cancelling forward-port of %s: inconsistent next target branch (%s)",
                    batch,
                    ', '.join(next_target.mapped('name')),
                )
                continue

            if n := self.with_context(active_test=False).search([
                ('target', '=', next_target.id),
                ('parent_id', '=', batch.id),
            ], limit=1):
                _logger.info('-> already forward-ported %s => %s', ident, n)
                continue

            if self.env['forwardport.batches'].search_count([('batch_id', '=', batch.id)]):
                _logger.warning('-> already recorded forward port for %s', ident)
                continue

            _logger.info('-> ok forward porting %s', ident)
            self.env['forwardport.batches'].create({
                'batch_id': batch.id,
                'source': 'fp',
            })
            scheduled |= batch
        return scheduled

    def _find_next_target(self):
        """Retrieves the next target from every PR, and returns it if it's the
        same for all the PRs which have one (PRs without a next target are
        ignored, this is considered acceptable).

        If the next targets are inconsistent, returns no next target.
        """
        next_target = self._find_next_targets()
        if len(next_target) == 1:
            return next_target
        else:
            return self.env['runbot_merge.branch'].browse(())

    def _find_next_targets(self):
        return self.prs.mapped(lambda p: p._find_next_target() or self.env['runbot_merge.branch'])

    def write(self, vals):
        if vals.get('merge_date'):
            # TODO: remove condition when everything is merged
            remover = self.env.get('forwardport.branch_remover')
            if remover is not None:
                remover.create([
                    {'pr_id': p.id}
                    for b in self
                    if not b.merge_date
                    for p in b.prs
                ])

        match vals.get('fw_policy'):
            case 'skipmerge':
                nonskip = self.filtered(lambda b: b.fw_policy != 'skipmerge')
            case 'skipci':
                nonskip = self.filtered(lambda b: b.merge_date and b.fw_policy != 'skipci')
            case _:
                nonskip = self.browse(())
        super().write(vals)

        # If we changed the fw policy to one which may trigger forward ports,
        # do trigger those here.
        if nonskip:
            for tip in nonskip.mapped(lambda b: b.genealogy_ids[-1]):
                tip._schedule_fp_followup()

        return True

    @api.ondelete(at_uninstall=True)
    def _on_delete_clear_stagings(self):
        self.batch_staging_ids.unlink()

    def unlink(self):
        """
        batches can be unlinked if they:

        - have run out of PRs
        - and don't have a parent batch (which is not being deleted)
        - and don't have a child batch (which is not being deleted)

        this is to keep track of forward port histories at the batch level
        """
        unlinkable = self.filtered(
            lambda b: not (b.prs or (b.parent_id - self) or (self.search([('parent_id', '=', b.id)]) - self))
        )
        return super(Batch, unlinkable).unlink()
