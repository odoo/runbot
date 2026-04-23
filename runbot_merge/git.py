from __future__ import annotations

import contextlib
import dataclasses
import io
import itertools
import logging
import os
import pathlib
import re
import resource
import stat
import subprocess
from operator import methodcaller
from typing import Optional, TypeVar, Union, Sequence, Tuple, Dict, Iterator
from collections.abc import Iterable, Mapping, Callable

from odoo.tools.appdirs import user_cache_dir
from .github import MergeError, PrCommit

_logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace
    from opentelemetry.propagate import inject
    tracer = trace.get_tracer(__name__)

    def git_tracing_params() -> Iterator[str]:
        tracing = {}
        inject(tracing)
        return itertools.chain.from_iterable(
            ('-c', f'http.extraHeader={k}:{v}')
            for k, v in tracing.items()
        )
except ImportError:
    trace = tracer = inject = None
    def git_tracing_params() -> Iterator[str]:
        return iter(())

def source_url(repository) -> str:
    return 'https://{}@github.com/{}'.format(
        repository.project_id.github_token,
        repository.name,
    )

def fw_url(repository) -> str:
    return 'https://{}@github.com/{}'.format(
        repository.project_id.fp_github_token,
        repository.fp_remote_target,
    )

Authorship = Union[Tuple[str, str], Tuple[str, str, str]]

def get_local(repository, *, clone: bool = True) -> 'Optional[Repo]':
    repos_dir = pathlib.Path(user_cache_dir('mergebot'))
    repos_dir.mkdir(parents=True, exist_ok=True)
    # NB: `repository.name` is `$org/$name` so this will be a subdirectory, probably
    repo_dir = repos_dir / repository.name

    if repo_dir.is_dir():
        return git(repo_dir)
    elif clone:
        _logger.info("Cloning out %s to %s", repository.name, repo_dir)
        subprocess.run([
            'git', *git_tracing_params(), 'clone', '--bare',
            source_url(repository), str(repo_dir)
        ], check=True)
        # All this could probably be removed since the shift to explicit fetches
        # but not sure it's worth removing?
        repo = git(repo_dir)
        repo.config('--add', 'remote.origin.fetch', '+refs/heads/*:refs/heads/*')
        repo.config('--add', 'remote.origin.fetch', '^refs/heads/tmp.*')
        repo.config('--add', 'remote.origin.fetch', '^refs/heads/staging.*')
        return repo
    else:
        _logger.warning(
            "Unable to acquire %s: %s",
            repo_dir,
            "doesn't exist" if not repo_dir.exists()\
        else oct(stat.S_IFMT(repo_dir.stat().st_mode))
        )
        return None


ALWAYS = ('gc.auto=0', 'maintenance.auto=0')


def _bypass_limits():
    resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))


def git(directory: str) -> 'Repo':
    return Repo(directory, check=True)


StdOutValue = Union[bool, int, None, io.IOBase]
class Repo:
    def __init__(self, directory: str, **config: object) -> None:
        self._directory = str(directory)
        config.setdefault('stderr', subprocess.PIPE)
        self._config = config
        self._params = ()
        self.runner = subprocess.run

    def __getattr__(self, name: str) -> GitCommand:
        return GitCommand(self, name.replace('_', '-'))

    def _run(self, *args, **kwargs) -> subprocess.CompletedProcess:
        opts = {**self._config, **kwargs}
        args = tuple(itertools.chain(
            ('git', '-C', self._directory),
            itertools.chain.from_iterable(('-c', p) for p in self._params + ALWAYS),
            git_tracing_params(),
            args,
        ))
        try:
            return self.runner(args, preexec_fn=_bypass_limits, **opts)
        except subprocess.CalledProcessError as e:
            stream = e.stderr or e.stdout
            if stream:
                if isinstance(stream, bytes):
                    with contextlib.suppress(UnicodeDecodeError):
                        stream = stream.decode()
                _logger.error("git call error: %s", stream)
            raise

    def stdout(self, flag: StdOutValue = True) -> Repo:
        match flag:
            case True:
                return self.with_config(stdout=subprocess.PIPE)
            case False:
                return self.with_config(stdout=None)
            case f:
                return self.with_config(stdout=f)

    def check(self, flag: bool) -> Repo:
        return self.with_config(check=flag)

    def with_config(self, **kw) -> Repo:
        opts = {**self._config, **kw}
        r = Repo(self._directory, **opts)
        r._params = self._params
        return r

    def with_params(self, *args) -> Repo:
        r = self.with_config()
        r._params = args
        return r

    def clone(self, to: str, branch: Optional[str] = None) -> Repo:
        self._run(
            'clone',
            *([] if branch is None else ['-b', branch]),
            self._directory, to,
        )
        return Repo(to)

    def fetch_heads(self, repo, *specs) -> Iterator[str]:
        """Fetches the heads of all the specified revisions, and returns their
        oids.
        """
        r = self.stdout().with_config(check=True, encoding="utf-8").fetch(
            # as its name does not indicate, retrieves all relevant objects but
            # stops short of updating local refs, so
            # `fetch --dry-run refs/heads/master` will resolve and retrieve the
            # head of that branch on the specified remote, but not touch the
            # local master
            '--dry-run',
            # parseable output (see below), returns info even when nothing happened
            '--porcelain',
            source_url(repo),
            *specs,
            no_tags=True,
        )
        # <flag> <old-object-id> <new-object-id> <local-reference>
        # flag:
        #   ` ` fast forward
        #   `+` forced update
        #   `-` pruned (deleted?) ref
        #   `t` tag update
        #   `*` new ref
        #   `!` rejected ref / update failure
        #   `=` same ref, did not need fetching
        #
        # local-reference FETCH_HEAD is always a new ref (*)
        # up-to-date refs are only printed if `--verbose`
        for m in re.finditer(r'^\* 0{40} ([0-9a-f]{40}) FETCH_HEAD$', r.stdout, flags=re.MULTILINE):
            yield m[1]

    def fetchone(self, repo, branch: str) -> str:
        """Discovers, retrieves, and returns the oid of the current head of a
        remote branch, without updating the remote-tracking branch for it.
        """
        return next(self.fetch_heads(repo, f"refs/heads/{branch}"))

    def get_tree(self, rev: str) -> str:
        return self.stdout().with_config(check=True, encoding="utf-8")\
            .rev_parse(f'{rev}^{{tree}}')\
            .stdout.strip()

    def rebase(self, dest: str, commits: Sequence[PrCommit]) -> Tuple[str, Dict[str, str]]:
        """Implements rebase by hand atop plumbing so:

        - we can work without a working copy
        - we can track individual commits (and store the mapping)

        It looks like `--merge-base` is not sufficient for `merge-tree` to
        correctly keep track of history, so it loses contents. Therefore
        implement in two passes as in the github version.
        """
        repo = self.stdout().with_config(text=True, check=False)

        logger = _logger.getChild('rebase')
        if not commits:
            raise MergeError("PR has no commits")

        prev_tree = repo.get_tree(dest)
        prev_original_tree = repo.get_tree(commits[0]['parents'][0]["sha"])

        new_trees = []
        parent = dest
        for original in commits:
            if len(original['parents']) != 1:
                raise MergeError(
                    f"commits with multiple parents ({original['sha']}) can not be rebased, "
                    "either fix the branch to remove merges or merge without "
                    "rebasing")

            new_trees.append(check(repo.merge_tree(parent, original['sha'])).stdout.strip())
            # allow merging empty commits, but not empty*ing* commits while merging
            if prev_original_tree != original['commit']['tree']['sha']:
                if new_trees[-1] == prev_tree:
                    raise MergeError(
                        f"commit {original['sha']} results in an empty tree when "
                        f"merged, it is likely a duplicate of a merged commit, "
                        f"rebase and remove."
                    )

            parent = check(repo.commit_tree(
                tree=new_trees[-1],
                parents=[parent, original['sha']],
                message=f'temp rebase {original["sha"]}',
            )).stdout.strip()
            prev_tree = new_trees[-1]
            prev_original_tree = original['commit']['tree']['sha']

        mapping = {}
        for original, tree in zip(commits, new_trees):
            authorship = check(repo.show('--no-patch', '--pretty=%an%n%ae%n%ai%n%cn%n%ce', original['sha']))
            author_name, author_email, author_date, committer_name, committer_email =\
                authorship.stdout.splitlines()

            c = check(repo.commit_tree(
                tree=tree,
                parents=[dest],
                message=original['commit']['message'],
                author=(author_name, author_email, author_date),
                committer=(committer_name, committer_email),
            )).stdout.strip()

            logger.debug('copied %s to %s (parent: %s)', original['sha'], c, dest)
            dest = mapping[original['sha']] = c

        return dest, mapping

    def merge(self, c1: str, c2: str, msg: str, *, author: Tuple[str, str]) -> str:
        repo = self.stdout().with_config(text=True, check=False)

        t = repo.merge_tree(c1, c2)
        if t.returncode:
            raise MergeError(t.stderr)

        c = self.commit_tree(
            tree=t.stdout.strip(),
            message=msg,
            parents=[c1, c2],
            author=author,
        )
        if c.returncode:
            raise MergeError(c.stderr)
        return c.stdout.strip()

    def commit_tree(
        self, *, tree: str, message: str,
        parents: Sequence[str] = (),
        author: Optional[Authorship] = None,
        committer: Optional[Authorship] = None,
    ) -> subprocess.CompletedProcess:
        authorship = {}
        if author:
            authorship['GIT_AUTHOR_NAME'] = author[0]
            authorship['GIT_AUTHOR_EMAIL'] = author[1]
            if len(author) > 2:
                authorship['GIT_AUTHOR_DATE'] = author[2]
        if committer:
            authorship['GIT_COMMITTER_NAME'] = committer[0]
            authorship['GIT_COMMITTER_EMAIL'] = committer[1]
            if len(committer) > 2:
                authorship['GIT_COMMITTER_DATE'] = committer[2]

        return self.with_config(
            input=message,
            stdout=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                **authorship,
                # we don't want git to use the timezone of the machine it's
                # running on: previously it used the timezone configured in
                # github (?), which I think / assume defaults to a generic UTC
                'TZ': 'UTC',
            }
        )._run(
            'commit-tree',
            tree,
            '-F', '-',
            *itertools.chain.from_iterable(('-p', p) for p in parents),
        )

    def update_tree(self, tree: str, files: Mapping[str, Callable[[Self, str], bytes | None]]) -> str:
        repo = self.stdout().with_config(stderr=None, check=False, text=None, encoding=None)
        for f, c in files.items():
            new_contents = c(repo, f)
            if new_contents is None:
                continue

            oid = repo \
                .with_config(input=new_contents) \
                .hash_object("-w", "--stdin", "--path", f) \
                .stdout.decode().strip()

            # we need to rewrite every tree from the parent of `f`
            while f:
                f, _, local = f.rpartition("/")
                # tree to update, `{tree}:` works as an alias for tree
                lstree = repo.ls_tree(f"{tree}:{f}").stdout.decode().splitlines(keepends=False)
                new_tree = []
                seen = False
                for mode, typ, sha, name in map(methodcaller("split", None, 3), lstree):
                    if name == local:
                        sha = oid
                        seen = True
                    # tab before name is critical to the format
                    new_tree.append(f"{mode} {typ} {sha}\t{name}\n")
                if not seen:
                    new_tree.append(f"100644 blob {oid}\t{local}\n")
                oid = repo.with_config(input="".join(new_tree).encode(), check=True).mktree().stdout.decode().strip()
            tree = oid
        return tree

    def modify_delete(self, tree: str, files: Iterable[str]) -> str:
        """Updates ``files`` in ``tree`` to add conflict markers to show them
        as being modify/delete-ed, rather than have only the original content.

        This is because having just content in a valid file is easy to miss,
        causing file resurrections as they get committed rather than re-removed.

        TODO: maybe extract the diff information compared to before they were removed? idk
        """
        def rewriter(r: Repo, f: str) -> bytes | None:
            contents = r.cat_file("-p", f"{tree}:{f}").stdout
            try:
                text = contents.decode()
            except UnicodeDecodeError:
                return None
            return f"""\
<<<\x3c<<< HEAD
||||||| MERGE BASE
=======
{text}
>>>\x3e>>> FORWARD PORTED
""".encode()
        return self.update_tree(tree, dict.fromkeys(files, rewriter))


def check(p: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    if not p.returncode:
        return p

    _logger.info("rebase failed at %s\nstdout:\n%s\nstderr:\n%s", p.args, p.stdout, p.stderr)
    raise MergeError(p.stderr or 'merge conflict')

@dataclasses.dataclass
class GitCommand:
    repo: Repo
    name: str

    if tracer:
        def __call__(self, *args, **kwargs) -> subprocess.CompletedProcess:
            with tracer.start_as_current_span(f"git.{self.name}", attributes={
                "http.target": None,
                "http.user_agent": "git",
            }):
                return self.repo._run(self.name, *args, *self._to_options(kwargs))
    else:
        def __call__(self, *args, **kwargs) -> subprocess.CompletedProcess:
            return self.repo._run(self.name, *args, *self._to_options(kwargs))

    def _to_options(self, d):
        for k, v in d.items():
            if len(k) == 1:
                yield '-' + k
            else:
                yield '--' + k.replace('_', '-')
            if v not in (None, True):
                assert v is not False
                yield str(v)
