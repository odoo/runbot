# -*- coding: utf-8 -*-
import logging
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
    ], required=True)

    def _process_item(self):
        batch = self.batch_id

        newbatch = batch.prs._port_forward()
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

class UpdateQueue(models.Model, Queue):
    _name = 'forwardport.updates'
    _description = 'if a forward-port PR gets updated & has followups (cherrypick succeeded) the followups need to be updated as well'

    limit = 10

    original_root = fields.Many2one('runbot_merge.pull_requests')
    new_root = fields.Many2one('runbot_merge.pull_requests')

    def _process_item(self):
        previous = self.new_root
        with ExitStack() as s:
            for child in self.new_root._iter_descendants():
                # QUESTION: update PR to draft if there are conflicts?
                _, working_copy = previous._create_fp_branch(
                    child.target, child.refname, s)

                new_head = working_copy.stdout().rev_parse(child.refname).stdout.decode().strip()
                # update child's head to the head we're going to push
                child.with_context(ignore_head_update=True).head = new_head
                working_copy.push('-f', 'target', child.refname)
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
        return [('pr_id.write_date', '<', cutoff)]

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
