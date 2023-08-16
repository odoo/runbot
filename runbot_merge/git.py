import dataclasses
import itertools
import logging
import pathlib
import resource
import subprocess
from typing import Optional, TypeVar

from odoo.tools.appdirs import user_cache_dir

_logger = logging.getLogger(__name__)


def source_url(repository, prefix: str) -> str:
    return 'https://{}@github.com/{}'.format(
        repository.project_id[f'{prefix}_token'],
        repository.name,
    )


def get_local(repository, prefix: Optional[str]) -> 'Optional[Repo]':
    repos_dir = pathlib.Path(user_cache_dir('mergebot'))
    repos_dir.mkdir(parents=True, exist_ok=True)
    # NB: `repository.name` is `$org/$name` so this will be a subdirectory, probably
    repo_dir = repos_dir / repository.name

    if repo_dir.is_dir():
        return git(repo_dir)
    elif prefix:
        _logger.info("Cloning out %s to %s", repository.name, repo_dir)
        subprocess.run(['git', 'clone', '--bare', source_url(repository, prefix), str(repo_dir)], check=True)
        # bare repos don't have fetch specs by default, and fetching *into*
        # them is a pain in the ass, configure fetch specs so `git fetch`
        # works properly
        repo = git(repo_dir)
        repo.config('--add', 'remote.origin.fetch', '+refs/heads/*:refs/heads/*')
        # negative refspecs require git 2.29
        repo.config('--add', 'remote.origin.fetch', '^refs/heads/tmp.*')
        repo.config('--add', 'remote.origin.fetch', '^refs/heads/staging.*')
        return repo


ALWAYS = ('gc.auto=0', 'maintenance.auto=0')


def _bypass_limits():
    resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))


def git(directory: str) -> 'Repo':
    return Repo(directory, check=True)


Self = TypeVar("Self", bound="Repo")
class Repo:
    def __init__(self, directory, **config) -> None:
        self._directory = str(directory)
        config.setdefault('stderr', subprocess.PIPE)
        self._config = config
        self._params = ()

    def __getattr__(self, name: str) -> 'GitCommand':
        return GitCommand(self, name.replace('_', '-'))

    def _run(self, *args, **kwargs) -> subprocess.CompletedProcess:
        opts = {**self._config, **kwargs}
        args = ('git', '-C', self._directory)\
            + tuple(itertools.chain.from_iterable(('-c', p) for p in self._params + ALWAYS))\
            + args
        try:
            return subprocess.run(args, preexec_fn=_bypass_limits, **opts)
        except subprocess.CalledProcessError as e:
            stream = e.stderr or e.stdout
            if stream:
                _logger.error("git call error: %s", stream)
            raise

    def stdout(self, flag: bool = True) -> Self:
        if flag is True:
            return self.with_config(stdout=subprocess.PIPE)
        elif flag is False:
            return self.with_config(stdout=None)
        return self.with_config(stdout=flag)

    def check(self, flag: bool) -> Self:
        return self.with_config(check=flag)

    def with_config(self, **kw) -> Self:
        opts = {**self._config, **kw}
        r = Repo(self._directory, **opts)
        r._params = self._params
        return r

    def with_params(self, *args) -> Self:
        r = self.with_config()
        r._params = args
        return r

    def clone(self, to: str, branch: Optional[str] = None) -> Self:
        self._run(
            'clone',
            *([] if branch is None else ['-b', branch]),
            self._directory, to,
        )
        return Repo(to)


@dataclasses.dataclass
class GitCommand:
    repo: Repo
    name: str

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
