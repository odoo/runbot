import logging
import re
from collections.abc import Iterator, Iterable

from odoo import models


_logger = logging.getLogger(__name__)
class BranchCleanup(models.TransientModel):
    _name = 'runbot_merge.branch_cleanup'
    _description = "cleans up scratch refs for deactivated branches"

    def _run(self):
        domain = [('active', '=', False)]
        if lastcall := self.env.context['lastcall']:
            domain.append(('write_date', '>=', lastcall))
        deactivated = self.env['runbot_merge.branch'].search(domain)

        _logger.info(
            "deleting scratch (tmp and staging) refs for branches %s",
            ', '.join(b.name for b in deactivated)
        )
        # loop around the repos first, so we can reuse the gh instance
        for r in deactivated.mapped('project_id.repo_ids'):
            gh = r.github()
            refs = gh('get', 'git/matching-refs/heads/')
            if refs.status_code != 200:
                _logger.warning("unable to fetch refs/heads for %s", r.name)
                continue
            head_pattern = re.compile(
                'refs/heads/'
                +
                ''.join(pattern_to_filter(
                    r.project_id.staging_pattern,
                    {
                        b.name
                        for b in deactivated
                        if b.project_id == r.project_id
                    }),
                )
            )
            for ref in refs.json():
                refname = ref['ref']
                if not head_pattern.fullmatch(refname):
                    continue

                res = gh('delete', f'git/{refname}', check=False)
                if res.status_code != 204:
                    branchname = refname.removeprefix('refs/heads/')
                    _logger.info("no branch found for %s:%s", r.name, branchname)


def pattern_to_filter(pattern: str, branches: Iterable[str]) -> Iterator[str]:
    pos = 0
    while (idx := pattern.find('%', pos)) != -1:
        if idx > pos:
            yield re.escape(pattern[pos:idx])
        match pattern[idx+1]:
            case '%':
                yield '%'
                pos = idx+2
            case '(':
                conv = pattern.index(')', idx+2) + 1
                assert pattern[conv] == 's'
                match pattern[idx+2:conv-1]:
                    case 'stage':
                        yield '(tmp|staging)'
                    case 'sub':
                        yield ''
                    case 'target':
                        yield '(' + '|'.join(map(re.escape, branches)) + ')'
                pos = conv+1
            case p:
                raise NotImplementedError(f"Unsupported printf form {p}")

    yield re.escape(pattern[pos:])