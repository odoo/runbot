# -*- coding: utf-8 -*-
import logging
import re

import requests

from odoo import fields, models

from ... import git

FOOTER = '\nMore info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port\n'

_logger = logging.getLogger(__name__)


class ForwardPortTasks(models.Model):
    _name = 'forwardport.batches'
    _inherit = ['runbot_merge.queue.retryable']
    _description = 'Check merged batches to forward port'
    _cron_name = 'runbot_merge.port_forward'
    _logger = _logger
    # retry every hour for a day
    RETRY_DELAY = 60
    RETRY_LIMIT = 24

    batch_id = fields.Many2one('runbot_merge.batch', required=True, index=True)
    source = fields.Selection([
        ('merge', 'Merge'),
        ('fp', 'Forward Port Followup'),
        ('insert', 'New branch port'),
        ('complete', 'Complete ported batches'),
    ], required=True)
    pr_id = fields.Many2one('runbot_merge.pull_requests')

    def _process_item(self):
        batch = self.batch_id
        if self.source == 'complete':
            self._complete_batches()
            return

        newbatch = batch._port_forward()
        if not newbatch:  # reached end of seq (or batch is empty)
            # FIXME: or configuration is fucky so doesn't want to FP (maybe should error and retry?)
            _logger.info(
                "Processed %s from %s (%s) -> end of the sequence",
                batch, self.source, batch.prs.mapped('display_name'),
            )
            return

        _logger.info(
            "Processed %s from %s (%s) -> %s (%s)",
            batch, self.source, ', '.join(batch.prs.mapped('display_name')),
            newbatch, ', '.join(newbatch.prs.mapped('display_name')),
        )
        # insert new batch in ancestry sequence
        if self.source == 'insert':
            self._process_insert(batch, newbatch)

    def _process_insert(self, batch, newbatch):
        self.env['runbot_merge.batch'].search([
            ('parent_id', '=', batch.id),
            ('id', '!=', newbatch.id),
        ]).parent_id = newbatch.id
        # insert new PRs in ancestry sequence unless conflict (= no parent)
        for pr in newbatch.prs:
            next_target = pr._find_next_target()
            if not next_target:
                continue

            # should have one since it was inserted before an other PR?
            descendant = pr.search([
                ('target', '=', next_target.id),
                ('source_id', '=', pr.source_id.id),
            ])

            # copy the reviewing of the "descendant" (even if detached) to this pr
            if reviewer := descendant.reviewed_by:
                pr.reviewed_by = reviewer

            # replace parent_id *if not detached*
            if descendant.parent_id:
                descendant.parent_id = pr.id

    def _search_domain(self):
        return [('pr_id.repository.project_id.disable_forwardport', '=', False)]

    def _complete_batches(self):
        source = pr = self.pr_id
        source_id = pr.source_id or pr
        if not pr:
            _logger.warning(
                "Unable to complete descendants of %s (%s): no new PR",
                self.batch_id,
                self.batch_id.prs.mapped('display_name'),
            )
            return
        _logger.info(
            "Completing batches for descendants of %s (added %s)",
            self.batch_id.prs.mapped('display_name'),
            self.pr_id.display_name,
        )

        gh = requests.Session()
        repository = pr.repository
        gh.headers['Authorization'] = f'token {repository.project_id.fp_github_token}'
        PullRequests = self.env['runbot_merge.pull_requests']
        self.env.cr.execute('LOCK runbot_merge_pull_requests IN SHARE MODE')

        # TODO: extract complete list of targets from `_find_next_target`
        #       so we can create all the forwardport branches, push them, and
        #       only then create the PR objects
        # TODO: maybe do that after making forward-port WC-less, so all the
        #       branches can be pushed atomically at once
        for descendant in self.batch_id.descendants():
            target = pr._find_next_target()
            if target is None:
                _logger.info("Will not forward-port %s: no next target", pr.display_name)
                return

            if PullRequests.search_count([
                ('source_id', '=', source_id.id),
                ('target', '=', target.id),
                ('state', 'not in', ('closed', 'merged')),
            ], limit=1):
                _logger.warning("Will not forward-port %s: already ported", pr.display_name)
                return

            if target != descendant.target:
                self.env['runbot_merge.pull_requests.feedback'].create({
                    'repository': repository.id,
                    'pull_request': source.number,
                    'token_field': 'fp_github_token',
                    'message': """\
{pr.ping}unable to port this PR forwards due to inconsistency: goes from \
{pr.target.name} to {next_target.name} but {batch} ({batch_prs}) targets \
{batch.target.name}.
""".format(pr=pr, next_target=target, batch=descendant, batch_prs=', '.join(descendant.mapped('prs.display_name')))
                })
                return

            ref = descendant.prs[:1].refname
            # NOTE: ports the new source everywhere instead of porting each
            #       PR to the next step as it does not *stop* on conflict
            repo = git.get_local(source.repository)
            conflict, head, n = source._create_port_branch(repo, target, forward=True)
            repo.push(git.fw_url(pr.repository), f'{head}:refs/heads/{ref}')

            remote_target = repository.fp_remote_target
            owner, _ = remote_target.split('/', 1)
            message = source.message + f"\n\nForward-Port-Of: {pr.display_name}"

            title, body = re.fullmatch(r'(?P<title>[^\n]+)\n*(?P<body>.*)', message, flags=re.DOTALL).groups()
            r = gh.post(f'https://api.github.com/repos/{pr.repository.name}/pulls', json={
                'base': target.name,
                'head': f'{owner}:{ref}',
                'title': title,
                'body': body
            })
            if not r.ok:
                _logger.warning("Failed to create forward-port PR for %s, deleting branches", pr.display_name)
                # delete all the branches this should automatically close the
                # PRs if we've created any. Using the API here is probably
                # simpler than going through the working copies
                d = gh.delete(f'https://api.github.com/repos/{remote_target}/git/refs/heads/{ref}')
                if d.ok:
                    _logger.info("Deleting %s:%s=success", remote_target, ref)
                else:
                    _logger.warning("Deleting %s:%s=%s", remote_target, ref, d.text)
                raise RuntimeError(f"Forwardport failure: {pr.display_name} ({r.text})")

            report_conflicts = conflict and self.batch_id.fw_policy != 'skipmerge'
            new_pr = PullRequests._from_gh(
                r.json(),
                batch_id=descendant.id,
                merge_method=pr.merge_method,
                source_id=source_id.id,
                parent_id=False if report_conflicts else pr.id,
                detach_reason="{1}\n{2}".format(*conflict).strip() if report_conflicts else None,
                squash=n==1,
            )
            _logger.info("Created forward-port PR %s", new_pr.display_name)

            if report_conflicts:
                self.env.ref('runbot_merge.forwardport.failure.conflict')._send(
                    repository=pr.repository,
                    pull_request=pr.number,
                    token_field='fp_github_token',
                    format_args={'source': source, 'pr': pr._suppress_ping(), 'new': new_pr, 'footer': FOOTER},
                )
            new_pr._fp_conflict_feedback(pr, {pr: conflict})

            labels = ['forwardport']
            if conflict:
                labels.append('conflict')
            self.env['runbot_merge.pull_requests.tagging'].create({
                'repository': new_pr.repository.id,
                'pull_request': new_pr.number,
                'tags_add': labels,
            })

            pr = new_pr

