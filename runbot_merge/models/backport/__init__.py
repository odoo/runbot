import logging
import re
import secrets

import requests

from odoo import models, fields
from odoo.exceptions import UserError

from ..batch import Batch
from ..project import Project
from ..pull_requests import Repository
from ... import git

_logger = logging.getLogger(__name__)


class PullRequest(models.Model):
    _inherit = 'runbot_merge.pull_requests'

    id: int
    display_name: str
    project: Project
    repository: Repository
    batch_id: Batch

    def backport(self) -> dict:
        if len(self) != 1:
            raise UserError(f"Backporting works one PR at a time, got {len(self)}")

        if len(self.batch_id.prs) > 1:
            raise UserError("Automatic backport of multi-pr batches is not currently supported")

        repo = self.repository
        if not self.project.github_id._fp_token(repo.name):
            raise UserError(f"Can not backport {self.display_name}: no token on project {self.project.display_name}")

        if not repo.fp_remote_target:
            raise UserError(f"Can not backport {self.display_name}: no remote on {repo.name}")

        w = self.env['runbot_merge.pull_requests.backport'].create({
            'pr_id': self.id,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': f"Backport of {self.display_name}",
            'views': [(False, 'form')],
            'target': 'new',
            'res_model': w._name,
            'res_id': w.id,
        }

class PullRequestBackport(models.TransientModel):
    _name = 'runbot_merge.pull_requests.backport'
    _description = "PR backport wizard"
    _rec_name = 'pr_id'

    pr_id = fields.Many2one('runbot_merge.pull_requests', required=True)
    project_id = fields.Many2one(related='pr_id.repository.project_id')
    source_seq = fields.Integer(related='pr_id.target.sequence')
    target = fields.Many2one(
        'runbot_merge.branch',
        domain="[('project_id', '=', project_id), ('sequence', '>', source_seq)]",
    )

    def action_apply(self) -> dict:
        if not self.target:
            raise UserError("A backport needs a backport target")

        project = self.pr_id.project
        branches = project._forward_port_ordered().ids
        source = self.pr_id.source_id or self.pr_id
        source_idx = branches.index(source.target.id)
        if branches.index(self.target.id) >= source_idx:
            raise UserError(
                "The backport branch needs to be before the source's branch "
                f"(got {self.target.name!r} and {source.target.name!r})"
            )

        _logger.info(
            "backporting %s (on %s) to %s",
            self.pr_id.display_name,
            self.pr_id.target.name,
            self.target.name,
        )

        bp_branch = f"{self.target.name}-{self.pr_id.refname}-{self.pr_id.batch_id.id}-bp"
        repo_id = self.pr_id.repository
        url, user, password = repo_id._fw_git()
        repo = git.get_local(repo_id).with_credentials(user, password)

        old_map = self.pr_id.commits_map
        self.pr_id.commits_map = "{}"
        conflict, head, n = self.pr_id._create_port_branch(repo, self.target, forward=False)
        self.pr_id.commits_map = old_map

        if conflict:
            feedback = "\n".join(filter(None, conflict[1:3]))
            raise UserError(f"backport conflict:\n\n{feedback}")
        repo.push(url, f"{head}:refs/heads/{bp_branch}")

        self.env.cr.execute('LOCK runbot_merge_pull_requests IN SHARE MODE')

        owner, _repo = repo_id.fp_remote_target.split('/', 1)
        message = source.message + f"\n\nBackport of {self.pr_id.display_name}"
        title, body = re.fullmatch(r'(?P<title>[^\n]+)\n*(?P<body>.*)', message, flags=re.DOTALL).groups()

        r = requests.post(
            f'https://api.github.com/repos/{repo_id.name}/pulls',
            headers={'Authorization': f'token {project.github_id._fp_token(repo_id.name)}'},
            json={
                'base': self.target.name,
                'head': f'{owner}:{bp_branch}',
                'title': title,
                'body': body
            }
        )
        if not r.ok:
            raise UserError(f"Backport PR creation failure: {r.text}")

        backport = self.env['runbot_merge.pull_requests']._from_gh(
            r.json(),
            merge_method=self.pr_id.merge_method,
            # the backport's own forwardport should stop right before the
            # original PR by default
            limit_id=branches[source_idx - 1],
            squash=n==1,
        )
        _logger.info("Created backport %s for %s", backport.display_name, self.pr_id.display_name)

        self.env['runbot_merge.pull_requests.tagging'].create({
            'repository': repo_id.id,
            'pull_request': backport.number,
            'tags_add': ['backport'],
        })
        # scheduling fp followup probably doesn't make sense since we don't copy the fw_policy...

        return {
            'type': 'ir.actions.act_window',
            'name': "new backport",
            'views': [(False, 'form')],
            'res_model': backport._name,
            'res_id': backport.id,
        }
