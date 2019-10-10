# -*- coding: utf-8 -*-
import logging
from contextlib import ExitStack

import subprocess

from odoo import fields, models


_logger = logging.getLogger(__name__)

class Queue:
    def _process_item(self):
        raise NotImplementedError

    def _process(self):
        for b in self.search([]):
            try:
                with self.env.cr.savepoint():
                    b._process_item()
                b.unlink()
                self.env.cr.commit()
            except Exception:
                _logger.exception("Error while processing %s, skipping", b)

class BatchQueue(models.Model, Queue):
    _name = 'forwardport.batches'
    _description = 'batches which got merged and are candidates for forward-porting'

    batch_id = fields.Many2one('runbot_merge.batch', required=True)
    source = fields.Selection([
        ('merge', 'Merge'),
        ('fp', 'Forward Port Followup'),
    ], required=True)

    def _process_item(self):
        batch = self.batch_id

        # only some prs of the batch have a parent, that's weird
        with_parent = batch.prs.filtered(lambda p: p.parent_id)
        if with_parent and with_parent != batch.prs:
            _logger.warning("Found a subset of batch %s (%s) with parents: %s, should probably investigate (normally either they're all parented or none are)", batch, batch.prs, with_parent)

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
