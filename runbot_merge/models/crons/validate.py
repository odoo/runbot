import contextlib
import json
import logging
try:
    from typing import Self
except ImportError:
    from typing_extensions import Self

from odoo import fields, models, api

from ... import git, exceptions
from ..stagings_create import StagingSlice, validate_pr, mismatch_warn

_logger = logging.getLogger(__name__)


class BatchValidate(models.Model):
    _name = 'runbot_merge.batch.validate'
    _descrition = "Post-ready / pre-staging validations"

    batch_id = fields.Many2one('runbot_merge.batch', index=True)

    @api.model_create_multi
    def create(self, vals_list: list[dict]) -> Self:
        self.env.ref('runbot_merge.cron_validate')\
            ._trigger_coalesced(factor=10)
        return super().create(vals_list)

    def _run(self) -> None:
        validations = self.search([])
        if blocked := validations.filtered(lambda v: v.batch_id.blocked):
            _logger.warning(
                "Found unexpected blocked batches in the validation queue: %s",
                ", ".join(blocked.batch_id)
            )
            validations -= blocked
            blocked.unlink()

        # NOTE: during a normal staging, we are *within* a branch,
        # so the staging state is just {repo: st}, here we're
        # dealing with every branch at once
        to_fetch = {}
        for pr in validations.batch_id.prs:
            if pr.repository not in to_fetch:
                to_fetch[pr.repository] = {
                    'repo': git.get_local(pr.repository).stdout().with_config(text=True),
                    'branches': {pr.target.name},
                    'heads': {pr.head},
                }
                continue
            to_fetch[pr.repository]['branches'].add(f"refs/heads/{pr.target.name}")
            to_fetch[pr.repository]['heads'].add(pr.head)

        states: dict[tuple[str, str], StagingSlice] = {}
        for repo, tasks in to_fetch.items():
            heads = tasks['heads']
            source = tasks['repo']
            # resolve and fetch separately because
            r = source.check(True).ls_remote(git.source_url(repo), *tasks['branches'])
            for line in r.stdout.splitlines(keepends=False):
                oid, _tab, ref = line.partition('\t')
                heads.add(oid)
                states[repo.name, ref.removeprefix('refs/heads/')] = StagingSlice(
                    gh=repo.github(),
                    head=oid,
                    repo=source.check(False),
                )
            source.check(True).fetch_heads(repo, *heads)

        for pr in validations.batch_id.prs:
            try:
                validate_pr(pr, states[pr.repository.name, pr.target.name])
            except exceptions.Mismatch as e:
                mismatch_warn(e, "data mismatch during check:\n{diff}")
            except exceptions.MergeError as e:
                if len(e.args) > 1 and e.args[1]:
                    reason = e.args[1]
                else:
                    reason = e.__cause__ or e.__context__
                with contextlib.suppress(Exception):
                    reason = json.loads(str(reason))['message'].lower()

                pr.error = True
                self.env.ref('runbot_merge.pr.merge.failed')._send(
                    repository=pr.repository,
                    pull_request=pr.number,
                    format_args={'pr': pr, 'reason': reason, 'exc': e},
                )

        validations.unlink()