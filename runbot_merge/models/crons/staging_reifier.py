""" Implements the reification of stagings into cross-repo git commits
"""
from __future__ import annotations

import abc
import datetime
import itertools
import json
from dataclasses import dataclass
from subprocess import PIPE
from types import SimpleNamespace
from typing import TypedDict

from odoo import api, fields, models
from ....runbot_merge import git
from ..batch import Batch
from ..pull_requests import Branch, StagingCommits


class Project(models.Model):
    _inherit = 'runbot_merge.project'

    repo_flat_layout = fields.Char()
    repo_pythonpath_layout = fields.Char()


class Stagings(models.Model):
    _inherit = 'runbot_merge.stagings'

    # can not make these required because we need to have created the staging
    # in order to know what its id is (also makes handling of batches easier)
    # and NOT NULL can't be deferred
    hash_flat_layout = fields.Char()
    hash_pythonpath_layout = fields.Char()

    id: int
    target: Branch
    batch_ids: Batch
    commits: StagingCommits
    staged_at: datetime.datetime


class Repository(models.Model):
    _inherit = 'runbot_merge.repository'

    name: str
    pythonpath_location = fields.Char(
        help="Where the submodule should be located in the pythonpath layout, "
             "if empty defaults to the root, unless `pythonpath_link_path` is "
             "set, then defaults to `.repos`."
             ""
             "%(fullname)s, %(owner)s, and %(name)s are available as"
             "placeholders in the path",
    )
    pythonpath_link_path = fields.Char(
        help="Where the repository should be symlinked in the pythonpath layout,"
             " leave empty to not symlink."
             ""
             "%(fullname)s, %(owner)s, and %(name)s are available as"
             "placeholders in the path",
    )
    pythonpath_link_target = fields.Char(
        help="Where the symlink should point to inside the repository, "
             "leave empty for the root",
    )

class Reifier(models.Model):
    _name = 'runbot_merge.staging.reifier'
    _description = "transcriptor of staging into cross-repo git commits"
    _order = 'create_date asc, id asc'

    staging_id: Stagings = fields.Many2one('runbot_merge.stagings', required=True)
    previous_staging_id: Stagings = fields.Many2one('runbot_merge.stagings')

    @api.model_create_multi
    def create(self, vals_list):
        self.env.ref('runbot_merge.staging_reifier')._trigger()
        return super().create(vals_list)

    def _run(self):
        projects = self.env['runbot_merge.project']
        branches = set()
        while r := self.search([], limit=1):
            reify(r.staging_id, r.previous_staging_id)
            projects |= r.staging_id.target.project_id
            branches.add(r.staging_id.target)
            r.unlink()
            self.env.cr.commit()

        for project in projects:
            if name_flat := project.repo_flat_layout:
                git.get_local(SimpleNamespace(
                    name=name_flat,
                    project_id=project,
                )).push('origin', 'refs/heads/*:refs/heads/*')
            if name_pythonpath := project.repo_pythonpath_layout:
                git.get_local(SimpleNamespace(
                    name=name_pythonpath,
                    project_id=project,
                )).push('origin', 'refs/heads/*:refs/heads/*')
def reify(staging: Stagings, prev: Stagings) -> None:
    project = staging.target.project_id
    repos = project.repo_ids.having_branch(staging.target)

    repo_flat = None
    commit_flat = prev.hash_flat_layout
    if name_flat := project.repo_flat_layout:
        repo_flat = git.get_local(SimpleNamespace(
            name=name_flat,
            project_id=project,
        )).with_config(check=True, encoding="utf-8", stdout=PIPE)

    repo_pythonpath = None
    commit_pythonpath = prev.hash_pythonpath_layout
    if name_pythonpath := project.repo_pythonpath_layout:
        repo_pythonpath = git.get_local(SimpleNamespace(
            name=name_pythonpath,
            project_id=project,
        )).with_config(check=True, encoding="utf-8", stdout=PIPE)

    if prev:
        commits = {c.repository_id.name: c.commit_id.sha for c in prev.commits}
    else:
        # if there is no `prev` then we need to run the staging in reverse in
        # order to get all the "root" commits for the staging, then we need to
        # get their parents in order to know what state the repos were in
        # before the staging, and *that* lets us figure out the per-batch state
        commits = {c.repository_id.name: c.commit_id.sha for c in staging.commits}

        # need to work off of the PR commits, because if no PR touches a
        # repository then `commits` could hold *the first commit in the
        # repository*, in which case we don't want to remove it
        pr_commits = {}
        for batch in reversed(staging.batch_ids):
            # FIXME: broken if the PR has more than one commit and was rebased...
            pr_commits.update(
                (pr.repository.name, json.loads(pr.commits_map)[''])
                for pr in batch.prs
            )

        for r, c in pr_commits.items():
            repo = git.get_local(SimpleNamespace(
                name=r,
                project_id=project,
            )).with_config(encoding="utf-8", stdout=PIPE)
            if parent := repo.rev_parse('--revs-only', c + '~').stdout.strip():
                commits[r] = parent
            else:
                # if a PR's commit has no parent then the PR itself introduced
                # the repo (what?) in which case we don't have the repo at the
                # staging and should ignore it
                del commits[r]

    for batch in staging.batch_ids:
        commits.update(
            (pr.repository.name, json.loads(pr.commits_map)[''])
            for pr in batch.prs
        )
        prs = batch.prs.sorted('repository').mapped('display_name')
        meta = json.dumps({
            'staging': staging.id,
            'batch': batch.id,
            'label': batch.name,
            'pull_requests': prs,
            'commits': commits,
        })

        message = f"{staging.id}.{batch.id}: {batch.name}\n\n- " + "\n- ".join(prs)
        if repo_flat:
            tree_flat = layout_flat(
                repo_flat, {'meta.json': GitBlob(meta)}, repos, commits)
            commit_flat = repo_flat.commit_tree(
                tree=tree_flat,
                message=message,
                parents=[commit_flat] if commit_flat else [],
                author=(project.github_name, project.github_email, staging.staged_at.isoformat(timespec='seconds')),
                committer=(project.github_name, project.github_email, staging.staged_at.isoformat(timespec='seconds')),
            ).stdout.strip()
        if repo_pythonpath:
            tree_pythonpath = layout_pythonpath(
                repo_pythonpath,
                {'meta.json': GitBlob(meta)},
                repos,
                commits,
            )
            commit_pythonpath = repo_pythonpath.commit_tree(
                tree=tree_pythonpath,
                message=message,
                parents=[commit_pythonpath] if commit_pythonpath else [],
                author=('robodoo', 'robodoo@odoo.com', staging.staged_at.isoformat(timespec='seconds')),
                committer=('robodoo', 'robodoo@odoo.com', staging.staged_at.isoformat(timespec='seconds')),
            ).stdout.strip()

    if repo_flat:
        repo_flat.update_ref(
            f'refs/heads/{staging.target.name}',
            commit_flat,
            prev.hash_flat_layout or '',
        )
    if repo_pythonpath:
        repo_pythonpath.update_ref(
            f'refs/heads/{staging.target.name}',
            commit_pythonpath,
            prev.hash_pythonpath_layout or '',
        )
    staging.write({
        'hash_flat_layout': commit_flat,
        'hash_pythonpath_layout': commit_pythonpath,
    })


def make_submodule(repo: str, path: str) -> str:
    _, n = repo.split('/')
    return f"""[submodule.{n}]
    path = {path}
    url = https://github.com/{repo}
"""

def layout_flat(
    repo: git.Repo,
    tree_init: dict[str, GitObject],
    repos: Repository,
    commits: dict[str, str],
) -> str:
    gitmodules = "\n".join(
        make_submodule(repo.name, repo.name.split('/')[-1])
        for repo in repos
        if repo.name in commits
    )

    return GitTree({
        **tree_init,
        ".gitmodules": GitBlob(gitmodules),
        **{
            repo.name.split('/')[-1]: GitCommit(cc)
            for repo in repos
            if (cc := commits.get(repo.name))
        }
    }).write(repo, 0)

def layout_pythonpath(
    repo: git.Repo,
    tree_init: dict[str, GitObject],
    repos: Repository,
    commits: dict[str, str],
) -> str:
    gitmodules = []
    tree = GitTree(tree_init)
    for repo_id in repos:
        commit = commits.get(repo_id.name)
        if commit is None:
            continue

        owner, name = repo_id.name.split('/')
        tmpl = {'fullname': repo_id.name, 'owner': owner, 'name': name}
        if p := repo_id.pythonpath_location:
            path = p % tmpl
        elif repo_id.pythonpath_link_path:
            path = ".repos/%(name)s" % tmpl
        else:
            path = '%(name)s' % tmpl
        link = (repo_id.pythonpath_link_path or '') % tmpl
        target = repo_id.pythonpath_link_target or ''

        tree.set(path, GitCommit(commit))
        gitmodules.append(make_submodule(repo_id.name, path))

        if link:
            if target:
                target = f"{path}/{target}"
            else:
                target = path
            tree.set(link, GitLink(target))

    tree.set(".gitmodules", GitBlob("\n".join(gitmodules)))
    return tree.write(repo, 0)

class Commit(TypedDict):
    staging_id: int
    commit_id: int
    repository_id: int

class GitObject(metaclass=abc.ABCMeta):
    @property
    @abc.abstractmethod
    def mode(self) -> str:
        ...

    @property
    @abc.abstractmethod
    def type(self) -> str:
        ...

    @abc.abstractmethod
    def write(self, repo: git.Repo, depth: int) -> str:
        ...

    def assert_tree(self) -> GitTree:
        assert isinstance(self, GitTree)
        return self

@dataclass
class GitCommit(GitObject):
    sha: str
    mode = "160000"
    type = "commit"

    def write(self, repo: git.Repo, _: int) -> str:
        return self.sha

@dataclass
class GitTree(GitObject):
    members: dict[str, GitObject]
    mode = "40000"
    type = "tree"

    def set(self, path: str, obj: GitObject) -> None:
        p, _, target = path.rpartition('/')
        assert target, f"{path!r} can't be empty"
        if p:
            for part in p.split('/'):
                self = self.members.setdefault(part, GitTree({})).assert_tree()
        self.members[target] = obj

    def write(self, repo: git.Repo, depth: int) -> str:
        assert all(self.members), \
            f"One of the tree entries has an empty filename {self.members}"
        return repo.with_config(input="".join(
            f"{obj.mode} {obj.type} {obj.write(repo, depth+1)}\t{name}\n"
            for name, obj in self.members.items()
        )).mktree().stdout.strip()

@dataclass
class GitLink(GitObject):
    reference: str
    mode = "120000"
    type = "blob"

    def write(self, repo: git.Repo, depth: int) -> str:
        target = "/".join(itertools.repeat("..", depth)) + "/" + self.reference
        return repo.with_config(input=target).hash_object("--stdin", "-w").stdout.strip()

@dataclass
class GitBlob(GitObject):
    content: str
    mode = "100644"
    type = "blob"

    def write(self, repo: git.Repo, _: int) -> str:
        return repo.with_config(input=self.content).hash_object('--stdin', '-w').stdout.strip()
