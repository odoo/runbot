# -*- coding: utf-8 -*-
import logging
import uuid
from contextlib import ExitStack
from datetime import datetime

from dateutil import relativedelta

from odoo import fields, models
from odoo.addons.runbot_merge.github import GH

# how long a merged PR survives
MERGE_AGE = relativedelta.relativedelta(weeks=2)

_logger = logging.getLogger(__name__)

class Queue:
    limit = 100

    def _process_item(self):
        raise NotImplementedError

    def _process(self):
        for b in self.search(self._search_domain(), order='create_date, id', limit=self.limit):
            try:
                b._process_item()
                b.unlink()
                self.env.cr.commit()
            except Exception:
                _logger.exception("Error while processing %s, skipping", b)
                self.env.cr.rollback()
            self.clear_caches()

    def _search_domain(self):
        return []

class BatchQueue(models.Model, Queue):
    _name = 'forwardport.batches'
    _description = 'batches which got merged and are candidates for forward-porting'

    limit = 10

    batch_id = fields.Many2one('runbot_merge.batch', required=True)
    source = fields.Selection([
        ('merge', 'Merge'),
        ('fp', 'Forward Port Followup'),
        ('insert', 'New branch port')
    ], required=True)

    def _process_item(self):
        batch = self.batch_id

        newbatch = batch.prs._port_forward()
        # insert new barch in ancestry sequence unless conflict (= no parent)
        if self.source == 'insert':
            for pr in newbatch.prs:
                if not pr.parent_id:
                    break
                newchild = pr.search([
                    ('parent_id', '=', pr.parent_id.id),
                    ('id', '!=', pr.id),
                ])
                if newchild:
                    newchild.parent_id = pr.id

        if newbatch:
            _logger.info(
                "Processing %s (from %s): %s (%s) -> %s (%s)",
                self.id, self.source,
                batch, batch.prs,
                newbatch, newbatch.prs,
            )
        else: # reached end of seq (or batch is empty)
            # FIXME: or configuration is fucky so doesn't want to FP (maybe should error and retry?)
            _logger.info(
                "Processing %s (from %s): %s (%s) -> end of the sequence",
                self.id, self.source,
                batch, batch.prs
            )
        batch.active = False


CONFLICT_TEMPLATE = "WARNING: the latest change ({previous.head}) triggered " \
                    "a conflict when updating the next forward-port " \
                    "({next.display_name}), and has been ignored.\n\n" \
                    "You will need to update this pull request differently, " \
                    "or fix the issue by hand on {next.display_name}."
CHILD_CONFLICT = "WARNING: the update of {previous.display_name} to " \
                 "{previous.head} has caused a conflict in this pull request, " \
                 "data may have been lost."
class UpdateQueue(models.Model, Queue):
    _name = 'forwardport.updates'
    _description = 'if a forward-port PR gets updated & has followups (cherrypick succeeded) the followups need to be updated as well'

    limit = 10

    original_root = fields.Many2one('runbot_merge.pull_requests')
    new_root = fields.Many2one('runbot_merge.pull_requests')

    def _process_item(self):
        Feedback = self.env['runbot_merge.pull_requests.feedback']
        previous = self.new_root
        with ExitStack() as s:
            for child in self.new_root._iter_descendants():
                _logger.info(
                    "Re-port %s from %s (changed root %s -> %s)",
                    child.display_name,
                    previous.display_name,
                    self.original_root.display_name,
                    self.new_root.display_name
                )
                if child.state in ('closed', 'merged'):
                    Feedback.create({
                        'repository': child.repository.id,
                        'pull_request': child.number,
                        'message': "Ancestor PR %s has been updated but this PR"
                                   " is %s and can't be updated to match."
                                   "\n\n"
                                   "You may want or need to manually update any"
                                   " followup PR." % (
                                       self.new_root.display_name,
                                       child.state,
                                   )
                    })
                    return

                conflicts, working_copy = previous._create_fp_branch(
                    child.target, child.refname, s)
                if conflicts:
                    _, out, err, _ = conflicts
                    Feedback.create({
                        'repository': previous.repository.id,
                        'pull_request': previous.number,
                        'message': CONFLICT_TEMPLATE.format(
                            previous=previous,
                            next=child
                        )
                    })
                    Feedback.create({
                        'repository': child.repository.id,
                        'pull_request': child.number,
                        'message': CHILD_CONFLICT.format(previous=previous, next=child)\
                            + (f'\n\nstdout:\n```\n{out.strip()}\n```' if out.strip() else '')
                            + (f'\n\nstderr:\n```\n{err.strip()}\n```' if err.strip() else '')
                    })

                new_head = working_copy.stdout().rev_parse(child.refname).stdout.decode().strip()
                commits_count = int(working_copy.stdout().rev_list(
                    f'{child.target.name}..{child.refname}',
                    count=True
                ).stdout.decode().strip())
                # update child's head to the head we're going to push
                child.with_context(ignore_head_update=True).write({
                    'head': new_head,
                    # 'state': 'opened',
                    'squash': commits_count == 1,
                })
                working_copy.push('-f', 'target', child.refname)

                # also push to local cache: looks like in some cases github
                # doesn't propagate revisions (?) or at least does so too slowly
                # so on the next loop we try to fetch the revision we just
                # pushed through PR and... we can't find it
                dummy_branch = str(uuid.uuid4())
                ref = previous._get_local_directory()
                working_copy.push(ref._directory, f'{new_head}:refs/heads/{dummy_branch}')
                ref.branch('--delete', '--force', dummy_branch)

                # committing here means github could technically trigger its
                # webhook before sending a response, but committing before
                # would mean we can update the PR in database but fail to
                # update on github, which is probably worse?
                # alternatively we can commit, push, and rollback if the push
                # fails
                # FIXME: handle failures (especially on non-first update)
                self.env.cr.commit()

                previous = child

_deleter = _logger.getChild('deleter')
class DeleteBranches(models.Model, Queue):
    _name = 'forwardport.branch_remover'
    _description = "Removes branches of merged PRs"

    pr_id = fields.Many2one('runbot_merge.pull_requests')

    def _search_domain(self):
        cutoff = self.env.context.get('forwardport_merged_before') \
             or fields.Datetime.to_string(datetime.now() - MERGE_AGE)
        return [('pr_id.merge_date', '<', cutoff)]

    def _process_item(self):
        _deleter.info(
            "PR %s: checking deletion of linked branch %s",
            self.pr_id.display_name,
            self.pr_id.label
        )

        if self.pr_id.state != 'merged':
            _deleter.info('✘ PR is not "merged" (got %s)', self.pr_id.state)
            return

        repository = self.pr_id.repository
        fp_remote = repository.fp_remote_target
        if not fp_remote:
            _deleter.info('✘ no forward-port target')
            return

        repo_owner, repo_name = fp_remote.split('/')
        owner, branch = self.pr_id.label.split(':')
        if repo_owner != owner:
            _deleter.info('✘ PR owner != FP target owner (%s)', repo_owner)
            return # probably don't have access to arbitrary repos

        github = GH(token=repository.project_id.fp_github_token, repo=fp_remote)
        refurl = 'git/refs/heads/' + branch
        ref = github('get', refurl, check=False)
        if ref.status_code != 200:
            _deleter.info("✘ branch already deleted (%s)", ref.json())
            return

        ref = ref.json()
        if isinstance(ref, list):
            _deleter.info(
                "✘ got a fuzzy match (%s), branch probably deleted",
                ', '.join(r['ref'] for r in ref)
            )
            return

        if ref['object']['sha'] != self.pr_id.head:
            _deleter.info(
                "✘ branch %s head mismatch, expected %s, got %s",
                self.pr_id.label,
                self.pr_id.head,
                ref['object']['sha']
            )
            return

        r = github('delete', refurl, check=False)
        assert r.status_code == 204, \
            "Tried to delete branch %s of %s, got %s" % (
                branch, self.pr_id.display_name,
                r.json()
            )
        _deleter.info('✔ deleted branch %s of PR %s', self.pr_id.label, self.pr_id.display_name)
