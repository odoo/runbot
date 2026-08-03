import collections
import logging
from typing import Mapping

from odoo import api, fields, models

from ... import git

Update = tuple[str, str, str]
Updates = list[Update]
class UpdateQueue(models.Model):
    _name = 'forwardport.updates'
    _inherit = ['runbot_merge.queue.retryable']
    _description = 'Update forward ports of an updated PR'
    _cron_name = 'runbot_merge.updates'
    _logger = logging.getLogger(__name__)

    original_root = fields.Many2one('runbot_merge.pull_requests')
    new_root = fields.Many2one('runbot_merge.pull_requests')

    def _process_item(self):
        # dict[repo: [ref, old_head, new_head]
        updates: Mapping[str, Updates] = collections.defaultdict[str, Updates](Updates)

        roots = self.new_root.batch_id.prs
        previouses = dict(zip(roots, roots))
        for batch in zip(*(
            root._iter_descendants()
            for root in roots
        )):
            if p := next(((r, c) for r, c in zip(roots, batch) if c.state in ('closed', 'merged')), None):
                root, child = p
                self.env.ref('runbot_merge.forwardport.updates.closed')._send(
                    repository=child.repository,
                    pull_request=child.number,
                    token_field='fp_github_token',
                    format_args={'pr': child, 'parent': root},
                )
                return

            for root, child in zip(roots, batch):
                original_root = self.original_root if root == self.new_root else root.root_id
                previous = previouses[root]
                self.env.cr.execute("""
                    SELECT id
                    FROM runbot_merge_pull_requests
                    WHERE id = %s
                    FOR UPDATE NOWAIT
                """, [child.id])
                self._logger.info(
                    "Re-port %s from %s (changed root %s -> %s)",
                    child.display_name,
                    previous.display_name,
                    original_root.display_name,
                    root.display_name,
                )

                repo = git.get_local(previous.repository)
                conflicts, new_head, n = previous._create_port_branch(repo, child.target, forward=True)

                if conflicts:
                    _, out, err, _ = conflicts
                    self.env.ref('runbot_merge.forwardport.updates.conflict.parent')._send(
                        repository=previous.repository,
                        pull_request=previous.number,
                        token_field='fp_github_token',
                        format_args={'pr': previous._suppress_ping(), 'next': child},
                    )
                    self.env.ref('runbot_merge.forwardport.updates.conflict.child')._send(
                        repository=child.repository,
                        pull_request=child.number,
                        token_field='fp_github_token',
                        format_args={
                            'previous': previous,
                            'pr': child._suppress_ping(),
                            'stdout': (f'\n\nstdout:\n```\n{out.strip()}\n```' if out.strip() else ''),
                            'stderr': (f'\n\nstderr:\n```\n{err.strip()}\n```' if err.strip() else ''),
                        },
                    )

                old_head = child.head
                # update child's head to the head we're going to push
                child.with_context(ignore_head_update=True).write({
                    'head': new_head,
                    'squash': n == 1,
                    'reviewed_by': False,
                    'statuses': '{}',
                    'error': False,
                })
                updates[child.repository].append((child.refname, old_head, new_head))

                previouses[root] = child

        for repository, refs in updates.items():
            # then update the child branches to the new heads
            git.get_local(repository).push(
                *(f'--force-with-lease={ref}:{old}' for ref, old, _new in refs),
                git.fw_url(repository),
                *(f"{new}:refs/heads/{ref}" for ref, _old, new in refs)
            )
