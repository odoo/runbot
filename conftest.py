from __future__ import annotations

import base64
import collections
import contextlib
import copy
import datetime
import errno
import fcntl
import functools
import http.client
import itertools
import json
import os
import pathlib
import random
import re
import secrets
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import typing
import uuid
import warnings
import xmlrpc.client
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit, quote

import pytest
import requests

collect_ignore = [
    p.name
    for p in pathlib.Path(__file__).parent.iterdir()
    if p.name not in ('runbot_merge', 'forwardport')
]

# When an operation can trigger webhooks, the test suite has to wait *some time*
# in the hope that the webhook(s) will have been dispatched by the end as github
# provides no real webhook feedback (e.g. an event stream).
#
# This also acts as a bit of a rate limiter, as github has gotten more and more
# angry with that. Especially around event-generating limits.
#
# TODO: explore https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
#       and see if it would be possible to be a better citizen (e.g. add test
#       retry / backoff using GH tighter GH integration)
WEBHOOK_WAIT_TIME = 10  # seconds
LOCAL_WEBHOOK_WAIT_TIME = 1

def pytest_addoption(parser):
    parser.addini('users', help="Path to a users json file.")
    parser.addoption('--users', help="Path to a users json file.")
    parser.addoption('--addons-path')
    parser.addoption("--no-delete", action="store_true", help="Don't delete repo after a failed run")
    parser.addoption('--log-github', action='store_true')
    parser.addoption('--coverage', action='store_true')

    parser.addoption(
        '--tunnel', action="store", default='',
        help="Tunneling script, should take a port as argv[1] and output the "
             "public address to stdout (with a newline) before closing it. "
             "The tunneling script should respond gracefully to SIGINT and "
             "SIGTERM.")

def is_manager(config: pytest.Config) -> bool:
    return not hasattr(config, 'workerinput')

def pytest_configure(config: pytest.Config) -> None:
    global WEBHOOK_WAIT_TIME
    # no tunnel => local setup, no need to wait as much
    if not config.getoption('--tunnel'):
        WEBHOOK_WAIT_TIME = LOCAL_WEBHOOK_WAIT_TIME

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'mergebot_test_utils'))
    config.addinivalue_line(
        "markers",
        "expect_log_errors(reason): allow and require tracebacks in the log",
    )

    if not config.getoption('--export-traces', None):
        return

    from opentelemetry import trace
    tracer = trace.get_tracer('mergebot-tests')

    # if the pytest-opentelemetry plugin is enabled hook otel into the test suite APIs
    # region enable requests for github calls
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    RequestsInstrumentor().instrument()
    # endregion

    # region hook opentelemetry into xmlrpc for Odoo RPC calls
    from opentelemetry.propagate import inject
    from opentelemetry.propagators.textmap import Setter
    # the default setter assumes a dict, but xmlrpc uses headers lists
    class ListSetter(Setter[list[tuple[str, str]]]):
        def set(self, carrier: list[tuple[str, str]], key: str, value: str) -> None:
            carrier.append((key, value))
    list_setter = ListSetter()

    wrapped_request = xmlrpc.client.Transport.request
    @functools.wraps(wrapped_request)
    def instrumented_request(self, host, handler, request_body, verbose=False):
        m = re.search(
            rb'<methodName>([^<]+)</methodName>',
            request_body,
        )
        if m[1] == b'authenticate':
            # ignore these because we spam authenticate to know when the server
            # is up (alternatively find a way to remove the span on auth error response)
            return wrapped_request(self, host, handler, request_body, verbose)
        if m[1] in (b'execute_kw', b'execute'):
            # dumbshit OdooRPC call, actual path is the combination of args 4 (object) and 5 (method)
            (_, _, _, objname, method, *_), _ = xmlrpc.client.loads(
                request_body,
                use_datetime=True,
                use_builtin_types=True,
            )
            span_name = f'{objname}.{method}()'
        else:
            span_name = m[1].decode()

        with tracer.start_as_current_span(span_name, kind=trace.SpanKind.CLIENT):
            return wrapped_request(self, host, handler, request_body, verbose)
    xmlrpc.client.Transport.request = instrumented_request

    # TODO: create a dedicated call span as the requests instrumentor does?
    #
    #       This is more complicated though because the request gets the
    #       serialized body so we'd need to get the methodname back out of the
    #       `request_body`... otoh that's just `<methodName>{name}</methodName>`
    wrapped_send_headers = xmlrpc.client.Transport.send_headers
    @functools.wraps(wrapped_send_headers)
    def instrumented_send_headers(self, connection: http.client.HTTPConnection, headers: list[tuple[str, str]]) -> None:
        inject(headers, setter=list_setter)
        wrapped_send_headers(self, connection, headers)
    xmlrpc.client.Transport.send_headers = instrumented_send_headers
    # endregion

def pytest_unconfigure(config: pytest.Config) -> None:
    if not is_manager(config):
        return

    if f := getattr(config, '_tmp_path_factory', None):
        for c in f.getbasetemp().iterdir():
            if c.is_file() and c.name == 'template':
                subprocess.run(['dropdb', '--if-exists', c.read_text(encoding='utf-8')])

@pytest.fixture(scope='session', autouse=True)
def _set_socket_timeout():
    """ Avoid unlimited wait on standard sockets during tests, this is mostly
    an issue for non-trivial cron calls
    """
    socket.setdefaulttimeout(120.0)

@pytest.fixture(scope="session")
def users_file(pytestconfig: pytest.Config) -> pathlib.Path:
    if p := pytestconfig.getoption('--users'):
        return pytestconfig.invocation_params.dir.joinpath(p)
    elif p := pytestconfig.getini('users'):
        return pytestconfig.inipath.parent.joinpath(p)
    else:
        raise ValueError("A users json file is required.")

@pytest.fixture(scope="session")
def config(users_file: pathlib.Path) -> dict[str, dict[str, str]]:
    """ Flat version of the pytest config file (pytest.ini), parses to a
    simple dict of {section: {key: value}}
    """
    with users_file.open('rb') as f:
        users = json.load(f)

    picks = []
    cnf = {}
    for k, e in users.items():
        val = {
            **e,
            'login': k,
            'token': (e.get('token') or [None])[0],
        }
        match r := e.get('role'):
            case 'user' | 'owner':
                cnf[f'role_{r}'] = val
            case _ if e['type'] == 'User':
                picks.append(val)
    cnf['github'] = dict(cnf['role_user'])
    if owner := cnf.get('role_owner'):
        cnf['github']['owner'] = owner['login']

    cnf.update(zip(
        ('role_reviewer', 'role_self_reviewer', 'role_other'),
        random.sample(picks, k=3),
    ))
    print("     reviewer:", cnf['role_reviewer']['login'])
    print("self-reviewer:", cnf['role_self_reviewer']['login'])
    print("        other:", cnf['role_other']['login'])

    return cnf

@pytest.fixture(scope='session')
def rolemap(request, config):
    # only fetch github logins once per session
    rolemap = {}
    for k, data in config.items():
        if not k.startswith('role_'):
            continue

        r = _rate_limited(lambda: requests.get(f'https://api.github.com/users/{data["login"]}'))
        r.raise_for_status()

        user = rolemap[k[5:]] = r.json()
        n = data['user'] = user['login']
        data.setdefault('name', n)
    return rolemap

@pytest.fixture
def partners(env, config, rolemap):
    """This specifically does not create partners for ``user`` and ``other``
    so they can be generated on-interaction, as "external" users.

    The two differ in that ``user`` has ownership of the org and can manage
    repos there, ``other`` is completely unrelated to anything so useful to
    check for interaction where the author only has read access to the reference
    repositories.
    """
    m = {}
    for role in ('reviewer', 'self_reviewer'):
        u = rolemap[role]
        login = u['login']
        conf = config[f'role_{role}']
        m[role] = env['res.partner'].create({
            'name': conf.get('name', login),
            'email': u['email'] or conf.get('email') or False,
            'github_login': login,
        })
    return m

@pytest.fixture
def setreviewers(partners):
    def _(*repos):
        partners['reviewer'].write({
            'review_rights': [
                (0, 0, {'repository_id': repo.id, 'review': True})
                for repo in repos
            ]
        })
        partners['self_reviewer'].write({
            'review_rights': [
                (0, 0, {'repository_id': repo.id, 'self_review': True})
                for repo in repos
            ]
        })
    return _

@pytest.fixture
def users(partners, rolemap) -> dict[str, str]:
    return {k: v['login'] for k, v in rolemap.items()}

@pytest.fixture(scope='session')
def tunnel(pytestconfig: pytest.Config, port: int) -> Iterator[str]:
    """ Creates a tunnel to localhost:<port>, should yield the
    publicly routable address & terminate the process at the end of the session
    """
    if tunnel := pytestconfig.getoption('--tunnel'):
        with subprocess.Popen(
            [tunnel, str(port)],
            stdout=subprocess.PIPE,
            encoding="utf-8",
        ) as p:
            # read() blocks forever and I don't know why, read things about the
            # write end of the stdout pipe still being open here?
            result = p.stdout.readline().strip()
            url = urlsplit(result)
            assert url.scheme and url.netloc
            yield url.geturl()
            p.terminate()
            p.wait(30)
    else:
        yield f'http://localhost:{port}'

@pytest.fixture(scope='session', autouse=True)
def http_proxy(pytestconfig: pytest.Config, users_file: pathlib.Path) -> Iterator[None]:
    if pytestconfig.getoption('--tunnel'):
        yield
        return

    # if we don't find dummy_central on the path, assume the environment is set
    # up externally *somehow* e.g. `HTTPS_PROXY` is set, and mitmdump (or some
    # other rewriting proxy) and DC (or equivalent) are running and properly
    # configured
    if not shutil.which('dummy_central'):
        yield
        return

    with contextlib.ExitStack() as cleanup:
        datadir = pathlib.Path(cleanup.enter_context(tempfile.TemporaryDirectory()))

        portfile = datadir / 'dcportfile'
        gh = cleanup.enter_context(subprocess.Popen(
            ['dummy_central', users_file, '-p', '0', '--portfile', portfile],
            stdout=sys.stdout.fileno(),
            stderr=sys.stderr.fileno(),
            env={**os.environ, 'RUST_BACKTRACE': '1'},
        ))
        cleanup.callback(gh.wait, 30)
        cleanup.callback(gh.terminate)

        gh_port = waitfile(portfile, lambda f: int(f.read_text()), ValueError, 300)
        gh_url = f'http://localhost:{gh_port}/'

        proxy = cleanup.enter_context(subprocess.Popen([
            'mitmdump',
            '-p', '0',
            # do not look up github's certificate details
            '--set', 'upstream_cert=false',
            # do not immediately connect to github (otherwise even with the
            # option above mitmproxy connects to github just in case)
            '--set', 'connection_strategy=lazy',
            # map github.com to our fake github
            '--map-remote', rf'|^https?://(\w+\.)?github.com/|{gh_url}',
            # write portfile once init completes
            '-s', pathlib.Path(__file__).parent / 'runbot_merge/portfile',
            '--set', f'datadir={datadir}',
        ],
            stdout=sys.stdout.fileno(),
            stderr=sys.stderr.fileno(),
        ))
        cleanup.callback(proxy.wait, 30)
        cleanup.callback(proxy.terminate)

        proxy_port = waitfile(
            datadir / 'portfile',
            lambda f: json.loads(f.read_text())['regular'],
            json.JSONDecodeError,
            timeout=30,
        )
        # writing our own env is icky but seems like the easiest way to
        # ensure the proxy configuration is picked up by urllib and requests
        # and subprocesses (git, odoo)
        os.environ['HTTPS_PROXY'] = f'http://localhost:{proxy_port}'

        yield

T = typing.TypeVar('T')
def waitfile(
    f: pathlib.Path,
    conv: typing.Callable[[pathlib.Path], T],
    conv_err: type,
    timeout: int = 10,
) -> T:
    """Waits until file `f` has been created and can be converted via `conv`,
    or timeout.

    `conv_err` allows setting a not-yet-convertible state for non-atomic file
    creations e.g. a json file created in-place in multiple `write(2)` calls in
    which case intermediate states should be ignored.
    """
    timeout = time.time() + timeout
    while time.time() < timeout:
        if f.is_file():
            break
        else:
            time.sleep(0.1)
    else:
        raise TimeoutError(f"Timed out waiting for {f}")

    while time.time() < timeout:
        try:
            return conv(f)
        except conv_err:
            time.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for {f}")


@pytest.fixture(scope='session')
def shared_dir(
        request: pytest.FixtureRequest,
        tmp_path_factory: pytest.TempPathFactory,
) -> pathlib.Path:
    shared_dir = tmp_path_factory.getbasetemp()
    if is_manager(request.config):
        return shared_dir

    # xdist workers get a subdir as their basetemp, so we need to go one
    # level up to deref it
    return shared_dir.parent

@contextlib.contextmanager
def xdist_lock(file: pathlib.Path) -> Generator[io.TextIOBase]:
    lockfile = os.open(file, os.O_CREAT | os.O_RDWR)
    fcntl.flock(lockfile, fcntl.LOCK_EX)

    with os.fdopen(lockfile, mode="r+", encoding='utf-8') as f:
        yield f

@pytest.fixture(scope='session')
def template_db(
        shared_dir: pathlib.Path,
        addons_path: str,
) -> str:
    """ Creates template DB once per run, then just duplicates it before
    starting odoo and running the testcase
    """
    with xdist_lock(shared_dir / 'template') as f:
        db = f.read()
        if db:
            return db

        d = (shared_dir / 'shared')
        try:
            d.mkdir()
        except FileExistsError:
            pytest.skip(f"found existing shared dir, database creation has likely failed")

        db = f'template-{secrets.token_hex(16)}'
        subprocess.run([
            'odoo', '--no-http',
            *(['--addons-path', addons_path] if addons_path else []),
            '-d', db, '-i', 'runbot_merge,saas_worker,auth_oauth',
            '--without-demo=all',
            '--max-cron-threads', '0',
            '--stop-after-init',
            '--log-level', 'warn',
            '--log-handler', 'py.warnings:ERROR',
        ],
            check=True,
            env={**os.environ, 'XDG_DATA_HOME': str(d)}
        )
        f.write(db)
        f.flush()
        os.fsync(f.fileno())
        subprocess.run(['psql', db, '-c', "UPDATE ir_cron SET nextcall = 'infinity'"])

        return db

@pytest.fixture
def db(
        request: pytest.FixtureRequest,
        shared_dir: pathlib.Path,
        template_db: str,
        tmp_path: pathlib.Path,
) -> Iterator[str]:
    rundb = f'mergebot-{secrets.token_hex(16)}'
    # createdb sometimes fails with "source database is being accessed by other
    # users". *We* have no connection (`template_db` has completed and the mutex
    # seems to work fine), and fetching the connected clients doesn't return
    # anything. This also is not consistent in time (relative to where we are
    # in the test suite). So I think this is something `postgres does
    # internally, for an instant, when creating a database from a template, and
    # if a concurrenty createdb starts at just the wrong time they conflict.
    #
    # In every occurrence I saw, the following attempt worked, so just attempt
    # it a few times.
    for _ in range(5):
        # createdb just returns 0/1 no details
        if subprocess.run(['createdb', '-T', template_db, rundb]).returncode == 0:
            break
    else:
        raise Exception("Failed to create test database")
    share = tmp_path.joinpath('share')
    share.mkdir()
    shutil.copytree(shared_dir / f'shared', share, dirs_exist_ok=True)
    (share / 'Odoo' / 'filestore' / template_db).rename(
        share / 'Odoo' / 'filestore' / rundb)

    yield rundb

    if not request.config.getoption('--no-delete'):
        subprocess.run(['dropdb', rundb], check=True)

def wait_for_hook():
    time.sleep(WEBHOOK_WAIT_TIME)

def wait_for_server(db: str, port: int, proc: subprocess.Popen, timeout: int = 120) -> None:
    """ Polls for server to be response & have installed our module.

    Raises socket.timeout on failure
    """
    limit = time.time() + timeout
    while True:
        if proc.poll() is not None:
            raise Exception("Server unexpectedly closed")

        try:
            uid = xmlrpc.client.ServerProxy(
                f'http://localhost:{port}/xmlrpc/2/common'
            ).authenticate(db, 'admin', 'admin', {
                'base_location': f"http://localhost:{port}",
            })
            mods = xmlrpc.client.ServerProxy(
                f'http://localhost:{port}/xmlrpc/2/object'
            ).execute_kw(
                db, uid, 'admin', 'ir.module.module', 'search_read', [
                    [('name', '=', 'runbot_merge')], ['state']
                ])
            if mods and mods[0].get('state') == 'installed':
                break
        except ConnectionRefusedError:
            if time.time() > limit:
                raise socket.timeout()

@pytest.fixture(scope='session')
def port():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(('', 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]

@pytest.fixture
def page(port):
    with requests.Session() as s:
        def get(url):
            r = s.get(f'http://localhost:{port}{url}')
            r.raise_for_status()
            return r.content
        yield get

@pytest.fixture(scope='session')
def dummy_addons_path(pytestconfig: pytest.Config) -> Iterator[str]:
    with tempfile.TemporaryDirectory() as dummy_addons_path:
        shutil.copytree(
            pathlib.Path(__file__).parent / 'mergebot_test_utils/saas_worker',
            pathlib.Path(dummy_addons_path, 'saas_worker'),
        )
        shutil.copytree(
            pathlib.Path(__file__).parent / 'mergebot_test_utils/saas_tracing',
            pathlib.Path(dummy_addons_path, 'saas_tracing'),
        )

        yield dummy_addons_path

@pytest.fixture(scope='session')
def addons_path(
        request: pytest.FixtureRequest,
        dummy_addons_path: str,
) -> str:
    return ','.join(map(str, filter(None, [
        request.config.getoption('--addons-path'),
        dummy_addons_path,
    ])))

@pytest.fixture
def server(
        request: pytest.FixtureRequest,
        db: str,
        port: int,
        addons_path: str,
        tmp_path: pathlib.Path,
) -> Iterator[tuple[subprocess.Popen, bytearray]]:
    log_handlers = [
        'odoo.modules.loading:WARNING',
        'py.warnings:ERROR',
    ]
    if not request.config.getoption('--log-github'):
        log_handlers.append('github_requests:WARNING')

    cov = []
    if request.config.getoption('--coverage'):
        cov = [
            'coverage', 'run',
            '-p', '--branch',
            '--source=odoo.addons.runbot_merge,odoo.addons.forwardport',
            '--context', request.node.nodeid,
            '-m',
        ]

    r, w = os.pipe2(os.O_NONBLOCK)
    buf = bytearray()
    def _move(inpt=r, output=sys.stdout.fileno()):
        while p.poll() is None:
            readable, _, _ = select.select([inpt], [], [], 1)
            if readable:
                r = os.read(inpt, 4096)
                if not r:
                    break
                try:
                    os.write(output, r)
                except OSError as e:
                    if e.errno == errno.EBADF:
                        break
                    raise
                buf.extend(r)
        os.close(inpt)

    CACHEDIR = tmp_path / 'cache'
    CACHEDIR.mkdir()
    BINDIR = tmp_path / 'bin'
    BINDIR.mkdir()
    subenv = {
        **os.environ,
        # stop putting garbage in the user dirs, and potentially creating conflicts
        # TODO: way to override this with macOS?
        'XDG_DATA_HOME': str(tmp_path / 'share'),
        'XDG_CACHE_HOME': str(CACHEDIR),
        'PATH': str(BINDIR) + os.pathsep + os.environ['PATH'],
    }
    serverwide = 'base,web'
    if request.config.getoption('--export-traces', None):
        serverwide = 'base,web,saas_tracing'
        # Inject OTEL context into subprocess env, so it can be extracted by
        # the server and the server setup (registry preload) is correctly nested
        # inside the test setup.
        from opentelemetry.propagate import inject
        inject(subenv)

    p = subprocess.Popen([
        *cov,
        'odoo', '--http-port', str(port),
        '--addons-path', addons_path,
        '-d', db,
        '--load', serverwide,
        '--max-cron-threads', '0', # disable cron threads (we're running crons by hand)
        *itertools.chain.from_iterable(('--log-handler', h) for h in log_handlers),
    ], stderr=w, env=subenv)
    os.close(w)
    # start the reader thread here so `_move` can read `p` without needing
    # additional handholding
    threading.Thread(target=_move, daemon=True).start()

    try:
        wait_for_server(db, port, p)

        yield p, buf
    finally:
        p.terminate()
        p.wait(timeout=30)

@pytest.fixture
def env(request, port, server, db):
    yield Environment(port, db)
    if request.node.get_closest_marker('expect_log_errors'):
        if b"Traceback (most recent call last):" not in server[1]:
            pytest.fail("should have found error in logs.")
    else:
        if b"Traceback (most recent call last):" in server[1]:
            pytest.fail("unexpected error in logs, fix, or mark function as `expect_log_errors` to require.")

@pytest.fixture
def reviewer_admin(env, partners):
    env['res.users'].create({
        'partner_id': partners['reviewer'].id,
        'login': 'reviewer',
        'groups_id': [
            (4, env.ref("base.group_user").id, 0),
            (4, env.ref("runbot_merge.group_admin").id, 0),
        ],
    })

def check(response):
    assert response.ok, response.text or response.reason
    return response

# users is just so I can avoid autouse on toplevel users fixture b/c it (seems
# to) break the existing local tests
@pytest.fixture
def make_repo(
        request: pytest.FixtureRequest,
        config: dict,
        tunnel: str,
        users: dict,
) -> Iterator[RepoMaker]:
    """Fixtures which creates a repository on the github side, plugs webhooks
    in, and registers the repository for deletion on cleanup (unless
    ``--no-delete`` is set)
    """
    owner = config['github']['owner']
    github = requests.Session()
    github.headers['Authorization'] = 'token %s' % config['github']['token']

    # check whether "owner" is a user or an org, as repo-creation endpoint is
    # different
    q = _rate_limited(lambda: github.get(f'https://api.github.com/users/{owner}'))
    q.raise_for_status()
    if q.json().get('type') == 'Organization':
        endpoint = f'https://api.github.com/orgs/{owner}/repos'
    else:
        endpoint = 'https://api.github.com/user/repos'
        r = check(github.get('https://api.github.com/user'))
        assert r.json()['login'] == owner

    repos = []
    def repomaker(name: str, *, hooks: bool = True) -> Repo:
        # create repo
        r = check(github.post(endpoint, json={
            'name': f'ignore_{name}_{secrets.token_urlsafe(6)}',
            'has_issues': True,
            'has_projects': False,
            'has_wiki': False,
            'auto_init': False,
            # at least one merge method must be enabled :(
            'allow_squash_merge': False,
            # 'allow_merge_commit': False,
            'allow_rebase_merge': False,
        }))
        r = r.json()
        repo_url = r['url']
        # wait for repository visibility
        while True:
            time.sleep(1)
            if github.head(repo_url).ok:
                break

        repo = Repo(github, r['full_name'], repos)

        if hooks:
            # create webhook
            check(github.post(r['hooks_url'], json={
                'name': 'web',
                'config': {
                    'url': '{}/runbot_merge/hooks'.format(tunnel),
                    'content_type': 'json',
                    'insecure_ssl': '1',
                },
                'events': ['pull_request', 'issue_comment', 'status', 'pull_request_review']
            }))
            time.sleep(1)

        check(github.put(r['contents_url'].replace('{+path}', 'a'), json={
            'path': 'a',
            'message': 'github returns a 409 (Git Repository is Empty) if trying to create a tree in a repo with no objects',
            'content': base64.b64encode(b'whee').decode('ascii'),
            'branch': f'garbage_{uuid.uuid4()}'
        }))
        time.sleep(1)
        return repo

    yield repomaker

    if not request.config.getoption('--no-delete'):
        for repo in reversed(repos):
            repo.delete()


class RepoMaker(typing.Protocol):
    def __call__(self, name: str, *, hooks: bool = True) -> Repo: ...


def _rate_limited(req):
    while True:
        q = req()
        if not q.ok and q.headers.get('X-RateLimit-Remaining') == '0':
            reset = int(q.headers['X-RateLimit-Reset'])
            delay = max(0, round(reset - time.time() + 1.0))
            time.sleep(delay)
            continue
        break
    return q


Commit = collections.namedtuple('Commit', 'id tree message author committer parents')


@dataclass
class Issue:
    repo: Repo
    token: str | None
    number: int

    @property
    def state(self) -> typing.Literal['open', 'closed']:
        r = self.repo._get_session(self.token)\
            .get(f'https://api.github.com/repos/{self.repo.name}/issues/{self.number}')
        assert r.ok, r.text
        state = r.json()['state']
        assert state in ('open', 'closed'), f"Invalid {state}, expected 'open' or 'closed'"
        return state


@pytest.fixture(scope='session')
def RepoType() -> type[Repo]:
    return Repo

class Repo:
    def __init__(self, session, fullname, repos):
        self._session = session
        self.name = fullname
        self._repos = repos
        self.hook = False
        repos.append(self)

    def __repr__(self):
        return f'<conftest.Repo {self.name}>'

    @property
    def owner(self):
        return self.name.split('/')[0]

    def unsubscribe(self, token=None):
        self._get_session(token).put(f'https://api.github.com/repos/{self.name}/subscription', json={
            'subscribed': False,
            'ignored': True,
        })

    def add_collaborator(self, login, token):
        # send invitation to user
        r = check(self._session.put(f'https://api.github.com/repos/{self.name}/collaborators/{login}'))
        # accept invitation on behalf of user
        check(requests.patch(f'https://api.github.com/user/repository_invitations/{r.json()["id"]}', headers={
            'Authorization': 'token ' + token
        }))
        # sanity check that user is part of collaborators
        r = check(self._session.get(f'https://api.github.com/repos/{self.name}/collaborators'))
        assert any(login == c['login'] for c in r.json())

    def _get_session(self, token):
        s = self._session
        if token:
            s = requests.Session()
            s.headers['Authorization'] = f'token {token}'
        return s

    def delete(self):
        r = self._session.delete(f'https://api.github.com/repos/{self.name}')
        if r.status_code != 204:
            warnings.warn("Unable to delete repository %s (HTTP %s)" % (self.name, r.status_code))

    def set_secret(self, secret):
        assert self.hook
        r = self._session.get(
            f'https://api.github.com/repos/{self.name}/hooks')
        assert 200 <= r.status_code < 300, r.text
        for hook in r.json():
            r = self._session.patch(f'https://api.github.com/repos/{self.name}/hooks/{hook["id"]}', json={
                'config': {**hook['config'], 'secret': secret},
            })
            assert 200 <= r.status_code < 300, r.text

    def get_ref(self, ref):
        if ref.startswith('refs/'):
            ref = ref[5:]
        if not ref.startswith('heads'):
            ref = 'heads/' + ref

        r = self._session.get(f'https://api.github.com/repos/{self.name}/git/ref/{ref}')
        assert 200 <= r.status_code < 300, r.reason or http.client.responses[r.status_code]
        res = r.json()
        assert res['object']['type'] == 'commit'
        return res['object']['sha']

    def commit(self, ref: str) -> Commit:
        if not re.match(r'[0-9a-f]{40}', ref):
            if not ref.startswith(('heads/', 'refs/heads/')):
                ref = 'refs/heads/' + ref
        # apparently heads/<branch> ~ refs/heads/<branch> but are not
        # necessarily up to date ??? unlike the git ref system where :ref
        # starts at heads/
        if ref.startswith('heads/'):
            ref = 'refs/' + ref

        r = self._session.get(f'https://api.github.com/repos/{self.name}/commits/{ref}')
        assert 200 <= r.status_code < 300, r.text

        return self._commit_from_gh(r.json())

    def _commit_from_gh(self, gh_commit: dict) -> Commit:
        c = gh_commit['commit']
        return Commit(
            id=gh_commit['sha'],
            tree=c['tree']['sha'],
            message=c['message'],
            author=c['author'],
            committer=c['committer'],
            parents=[p['sha'] for p in gh_commit['parents']],
        )

    def read_tree(self, commit: Commit, *, recursive=False) -> dict[str, str]:
        """ read tree object from commit
        """
        r = self._session.get(
            f'https://api.github.com/repos/{self.name}/git/trees/{commit.tree}',
            params={'recursive': '1'} if recursive else None,
        )
        assert 200 <= r.status_code < 300, r.text

        # read tree's blobs
        tree = {}
        for t in r.json()['tree']:
            match t['type']:
                case 'commit':
                    tree[t['path']] = f"@{t['sha']}"
                case 'blob':
                    r = self._session.get(f'https://api.github.com/repos/{self.name}/git/blobs/{t["sha"]}')
                    assert 200 <= r.status_code < 300, r.text
                    tree[t['path']] = base64.b64decode(r.json()['content'])
                    with contextlib.suppress(UnicodeDecodeError):
                        tree[t['path']] = tree[t['path']].decode()
                case 'tree' if not recursive:
                    tree[t['path']] = t['sha']

        return tree

    def make_ref(self, name, commit, force=False):
        assert self.hook
        assert name.startswith('heads/')
        r = self._session.post(f'https://api.github.com/repos/{self.name}/git/refs', json={
            'ref': 'refs/' + name,
            'sha': commit,
        })
        if force and r.status_code == 422:
            self.update_ref(name, commit, force=force)
            return
        assert r.ok, r.text

    def update_ref(self, name, commit, force=False):
        assert self.hook
        r = self._session.patch(f'https://api.github.com/repos/{self.name}/git/refs/{name}', json={'sha': commit, 'force': force})
        assert r.ok, r.text

    def delete_ref(self, name):
        assert self.hook
        r = self._session.delete(f'https://api.github.com/repos/{self.name}/git/refs/{name}')
        assert r.ok, r.text

    def protect(self, branch):
        assert self.hook
        r = self._session.put(f'https://api.github.com/repos/{self.name}/branches/{branch}/protection', json={
            'required_status_checks': None,
            'enforce_admins': True,
            'required_pull_request_reviews': None,
            'restrictions': None,
        })
        assert 200 <= r.status_code < 300, r.text

    def make_commits(self, root, *commits, ref=None, make=True):
        assert self.hook
        if isinstance(root, list):
            parents = root
            tree = None
        elif root:
            c = self.commit(root)
            tree = c.tree
            parents = [c.id]
        else:
            tree = None
            parents = []

        hashes = []
        for commit in commits:
            if commit.tree:
                if commit.reset:
                    tree = None
                tree_entries = []
                for k, v in commit.tree.items():
                    base_tree = {'path': k, 'mode': '100644', 'type': 'blob'}
                    if isinstance(v, bytes):
                        r = self._session.post(f"https://api.github.com/repos/{self.name}/git/blobs", json={
                            "content": base64.b64encode(v).decode(),
                            "encoding": "base64",
                        })
                        assert r.ok, r.text
                        base_tree['sha'] = r.json()['sha']
                    else:
                        base_tree['content'] = v
                    tree_entries.append(base_tree)
                r = self._session.post(f'https://api.github.com/repos/{self.name}/git/trees', json={
                    'tree': tree_entries,
                    'base_tree': tree
                })
                assert r.ok, r.text
                tree = r.json()['sha']

            data = {
                'parents': parents,
                'message': commit.message,
                'tree': tree,
            }
            if commit.author:
                data['author'] = commit.author
            if commit.committer:
                data['committer'] = commit.committer

            r = self._session.post(f'https://api.github.com/repos/{self.name}/git/commits', json=data)
            assert r.ok, r.text

            hashes.append(r.json()['sha'])
            parents = [hashes[-1]]

        if ref:
            fn = self.make_ref if make else self.update_ref
            fn(ref, hashes[-1], force=True)

        return hashes

    def fork(self, *, token=None):
        s = self._get_session(token)

        r = s.post(f'https://api.github.com/repos/{self.name}/forks', json={
            'default_branch_only': True,
        })
        assert r.ok, r.text

        response = r.json()
        repo_name = response['full_name']
        repo_url = response['url']
        # poll for end of fork
        limit = time.time() + 60
        while s.head(repo_url, timeout=5).status_code != 200:
            if time.time() > limit:
                raise TimeoutError(f"No response for repo {repo_name} over 60s")
            time.sleep(1)

        # wait for the branches (which should have been copied over) to be visible
        while not s.get(f'{repo_url}/branches').json():
            if time.time() > limit:
                raise TimeoutError(f"No response for repo {repo_name} over 60s")
            time.sleep(1)

        return Repo(s, repo_name, self._repos)

    def get_pr(self, number):
        # ensure PR exists before returning it
        self._session.head(f'https://api.github.com/repos/{self.name}/pulls/{number}').raise_for_status()
        return PR(self, number)

    def make_pr(
            self,
            *,
            title: Optional[str] = None,
            body: Optional[str] = None,
            target: str,
            head: str,
            draft: bool = False,
            token: Optional[str] = None
    ) -> PR:
        assert self.hook

        if title is None:
            assert ":" not in head, \
                "will not auto-infer titles for PRs in a remote repo"
            c = self.commit(head)
            parts = iter(c.message.split('\n\n', 1))
            title = next(parts)
            body = next(parts, None)

        # FIXME: change tests which pass a commit id to make_pr & remove this
        if re.match(r'[0-9a-f]{40}', head):
            ref = "temp_trash_because_head_must_be_a_ref_%d" % next(ct)
            self.make_ref('heads/' + ref, head)
            head = ref

        r = self._get_session(token).post(
            f'https://api.github.com/repos/{self.name}/pulls',
            json={
                'title': title,
                'body': body,
                'head': head,
                'base': target,
                'draft': draft,
            },
        )
        assert 200 <= r.status_code < 300, r.text
        time.sleep(1)
        return PR(self, r.json()['number'])

    def post_status(self, ref, status, context='default', **kw):
        assert self.hook
        assert status in ('error', 'failure', 'pending', 'success')
        commit = ref if isinstance(ref, Commit) else self.commit(ref)
        r = self._session.post(f'https://api.github.com/repos/{self.name}/statuses/{commit.id}', json={
            'state': status,
            'context': context,
            **kw
        })
        assert 200 <= r.status_code < 300, r.text

    def is_ancestor(self, sha, of):
        return any(c['sha'] == sha for c in self.log(of))

    def log(self, ref_or_sha):
        for page in itertools.count(1):
            r = self._session.get(
                f'https://api.github.com/repos/{self.name}/commits',
                params={'sha': ref_or_sha, 'page': page}
            )
            assert 200 <= r.status_code < 300, r.text
            yield from r.json()
            if not r.links.get('next'):
                return

    def make_issue(self, title, *, body=None, token=None) -> Issue:
        assert self.hook

        r = self._get_session(token).post(
            f"https://api.github.com/repos/{self.name}/issues",
            json={'title': title, 'body': body}
        )
        assert 200 <= r.status_code < 300, r.text
        return Issue(self, token, r.json()['number'])

    @contextlib.contextmanager
    def disable_hooks(self) -> typing.Iterator[None]:
        assert self.hook
        sess = self._get_session(None)
        r = sess.get(f'https://api.github.com/repos/{self.name}/hooks')
        assert r.ok, r.text
        hook_urls = [hook['url'] for hook in r.json() if hook['active']]
        for hook in hook_urls:
            r = sess.patch(hook, json={'active': False})
            assert r.ok, r.text
        wait_for_hook()
        yield
        wait_for_hook()
        for hook in reversed(hook_urls):
            r = sess.patch(hook, json={'active': True})
            assert r.ok, r.text

    def __enter__(self):
        self.hook = True
        return self
    def __exit__(self, *args):
        wait_for_hook()
        self.hook = False
    class Commit:
        def __init__(self, message, *, author=None, committer=None, tree, reset=False):
            self.id = None
            self.message = message
            self.author = author
            self.committer = committer
            self.tree = tree
            self.reset = reset
MakeCommit = Repo.Commit
ct = itertools.count()

class Comment(tuple):
    def __new__(cls, c):
        self = super(Comment, cls).__new__(cls, (c['user']['login'], c['body']))
        self._c = c
        return self
    def __getitem__(self, item):
        if isinstance(item, int):
            return super().__getitem__(item)
        return self._c[item]


PR_SET_READY = '''
mutation setReady($pid: ID!) {
    markPullRequestReadyForReview(input: { pullRequestId: $pid}) {
        clientMutationId
    }
}
'''

PR_SET_DRAFT = '''
mutation setDraft($pid: ID!) {
    convertPullRequestToDraft(input: { pullRequestId: $pid }) {
        clientMutationId
    }
}
'''
def state_prop(name: str) -> property:
    @property
    def _prop(self):
        return self._pr[name]
    return _prop.setter(lambda self, v: self._set_prop(name, v))

class PR:
    def __init__(self, repo, number):
        self.repo = repo
        self.number = number
        self.labels = LabelsProxy(self)
        self._cache = None, {}

    @property
    def _pr(self):
        previous, caching = self._cache
        r = self.repo._session.get(
            f'https://api.github.com/repos/{self.repo.name}/pulls/{self.number}',
            headers=caching
        )
        assert r.ok, r.text
        if r.status_code == 304:
            return previous
        contents, caching = self._cache = r.json(), {}
        if r.headers.get('etag'):
            caching['If-None-Match'] = r.headers['etag']
        if r.headers.get('last-modified'):
            caching['If-Modified-Since']= r.headers['Last-Modified']
        return contents

    title = state_prop('title')
    body = state_prop('body')
    base = state_prop('base')

    @property
    def draft(self):
        return self._pr['draft']
    @draft.setter
    def draft(self, v):
        assert self.repo.hook
        # apparently it's not possible to update the draft flag via the v3 API,
        # only the V4...
        r = self.repo._session.post('https://api.github.com/graphql', json={
            'query': PR_SET_DRAFT if v else PR_SET_READY,
            'variables': {'pid': self._pr['node_id']}
        })
        assert r.ok, r.text
        out = r.json()
        assert 'errors' not in out, out['errors']

    @property
    def head(self):
        return self._pr['head']['sha']

    @property
    def user(self):
        return self._pr['user']['login']

    @property
    def state(self):
        return self._pr['state']

    @property
    def comments(self):
        r = self.repo._session.get(f'https://api.github.com/repos/{self.repo.name}/issues/{self.number}/comments')
        assert 200 <= r.status_code < 300, r.text
        return [Comment(c) for c in r.json()]

    @property
    def ref(self):
        return 'heads/' + self.branch.branch

    def post_comment(self, body, token=None):
        assert self.repo.hook
        headers = {}
        if token:
            headers['Authorization'] = 'token %s' % token
        r = self.repo._session.post(
            f'https://api.github.com/repos/{self.repo.name}/issues/{self.number}/comments',
            json={'body': body},
            headers=headers,
        )
        assert 200 <= r.status_code < 300, r.text
        return r.json()['id']

    def edit_comment(self, cid, body, token=None):
        assert self.repo.hook
        headers = {}
        if token:
            headers['Authorization'] = f'token {token}'
        r = self.repo._session.patch(
            f'https://api.github.com/repos/{self.repo.name}/issues/comments/{cid}',
            json={'body': body},
            headers=headers
        )
        assert 200 <= r.status_code < 300, r.text

    def delete_comment(self, cid, token=None):
        assert self.repo.hook
        headers = {}
        if token:
            headers['Authorization'] = 'token %s' % token
        r = self.repo._session.delete(
            f'https://api.github.com/repos/{self.repo.name}/issues/comments/{cid}',
            headers=headers
        )
        assert r.status_code == 204, r.text

    def _set_prop(self, prop, value, token=None):
        assert self.repo.hook, "the repo must be hooked to perform modifications to a PR"
        headers = {}
        if token:
            headers['Authorization'] = 'token ' + token
        r = self.repo._session.patch(f'https://api.github.com/repos/{self.repo.name}/pulls/{self.number}', json={
            prop: value
        }, headers=headers)
        assert r.ok, r.text

    def open(self, token=None):
        self._set_prop('state', 'open', token=token)

    def close(self, token=None):
        self._set_prop('state', 'closed', token=token)

    @property
    def branch(self):
        r = self.repo._session.get(f'https://api.github.com/repos/{self.repo.name}/pulls/{self.number}')
        assert 200 <= r.status_code < 300, r.text
        info = r.json()

        repo = self.repo
        reponame = info['head']['repo']['full_name']
        if reponame != self.repo.name:
            # not sure deep copying the session object is safe / proper...
            repo = Repo(copy.deepcopy(self.repo._session), reponame, [])

        return PRBranch(repo, info['head']['ref'])

    def post_review(self, state, body, token=None):
        assert self.repo.hook
        headers = {}
        if token:
            headers['Authorization'] = f'token {token}'
        r = self.repo._session.post(
            f'https://api.github.com/repos/{self.repo.name}/pulls/{self.number}/reviews',
            json={'body': body, 'event': state,},
            headers=headers
        )
        assert 200 <= r.status_code < 300, r.text

PRBranch = collections.namedtuple('PRBranch', 'repo branch')
class LabelsProxy(collections.abc.MutableSet):
    def __init__(self, pr):
        self._pr = pr

    @property
    def _labels(self):
        pr = self._pr
        r = pr.repo._session.get(f'https://api.github.com/repos/{pr.repo.name}/issues/{pr.number}/labels')
        assert r.ok, r.text
        return {label['name'] for label in r.json()}

    def __repr__(self):
        return '<LabelsProxy %r>' % self._labels

    def __eq__(self, other):
        if isinstance(other, collections.abc.Set):
            return other == self._labels
        return NotImplemented

    def __contains__(self, label):
        return label in self._labels

    def __iter__(self):
        return iter(self._labels)

    def __len__(self):
        return len(self._labels)

    def add(self, label):
        pr = self._pr
        assert pr.repo.hook
        r = pr.repo._session.post(f'https://api.github.com/repos/{pr.repo.name}/issues/{pr.number}/labels', json={
            'labels': [label]
        })
        assert r.ok, r.text

    def discard(self, label):
        pr = self._pr
        assert pr.repo.hook
        r = pr.repo._session.delete(f'https://api.github.com/repos/{pr.repo.name}/issues/{pr.number}/labels/{label}')
        # discard should do nothing if the item didn't exist in the set
        assert r.ok or r.status_code == 404, r.text

    def update(self, *others):
        pr = self._pr
        assert pr.repo.hook
        # because of course that one is not provided by MutableMapping...
        r = pr.repo._session.post(f'https://api.github.com/repos/{pr.repo.name}/issues/{pr.number}/labels', json={
            'labels': list(set(itertools.chain.from_iterable(others)))
        })
        assert r.ok, r.text

class Environment:
    def __init__(self, port, db):
        self._port = port
        self._db = db
        self._uid = None
        self._password = None
        self._object = xmlrpc.client.ServerProxy(f'http://localhost:{port}/xmlrpc/2/object')
        self.login('admin', 'admin')
        self._context = {}

    @functools.cached_property
    def user(self):
        return Model(self, 'res.users', [self._uid])

    def with_user(self, login, password):
        env = copy.copy(self)
        env.login(login, password)
        return env

    def with_context(self, **kw):
        env = copy.copy(self)
        env._context = {**self._context, **kw}
        return env

    def login(self, login, password):
        self._password = password
        self._uid = xmlrpc.client.ServerProxy(
            f'http://localhost:{self._port}/xmlrpc/2/common'
        ).authenticate(self._db, login, password, {})

    def __call__(self, model, method, *args, **kwargs):
        kwargs['context'] = {**self._context, **kwargs.get('context', {})}
        return self._object.execute_kw(
            self._db, self._uid, self._password,
            model, method,
            args, kwargs
        )

    def __getitem__(self, name):
        return Model(self, name)

    def ref(self, xid, raise_if_not_found=True):
        model, obj_id = self(
            'ir.model.data', 'check_object_reference',
            *xid.split('.', 1),
            raise_on_access_error=raise_if_not_found
        )
        return Model(self, model, [obj_id]) if obj_id else None


    def run_crons(self, *xids, **kw):
        crons = xids or ['runbot_merge.check_linked_prs_status']
        cron_ids = []
        for xid in crons:
            if xid is None:
                continue

            model, cron_id = self('ir.model.data', 'check_object_reference', *xid.split('.', 1))
            assert model == 'ir.cron', f"Expected {xid} to be a cron, got {model}"
            cron_ids.append(cron_id)
        if cron_ids:
            self('ir.cron', 'write', cron_ids, {
                'nextcall': (datetime.datetime.utcnow() - datetime.timedelta(seconds=30)).isoformat(" ", "seconds")
            }, **kw)
        self('base', 'run_crons', [], **kw)
        # sleep for some time as a lot of crap may have happened (?)
        wait_for_hook()

class Model:
    __slots__ = ['env', '_name', '_ids', '_fields']
    def __init__(self, env, model, ids=(), fields=None):
        object.__setattr__(self, 'env', env)
        object.__setattr__(self, '_name', model)
        object.__setattr__(self, '_ids', tuple(ids or ()))

        object.__setattr__(self, '_fields', fields or self.env(self._name, 'fields_get', attributes=['type', 'relation']))

    @property
    def ids(self):
        return self._ids

    @property
    def _env(self): return self.env

    @property
    def _model(self): return self._name

    def __bool__(self):
        return bool(self._ids)

    def __len__(self):
        return len(self._ids)

    def __hash__(self):
        return hash((self._model, frozenset(self._ids)))

    def __eq__(self, other):
        if not isinstance(other, Model):
            return NotImplemented
        return self._model == other._model and set(self._ids) == set(other._ids)

    def __repr__(self):
        return f"{self._model}({', '.join(map(str, self._ids))})"

    # method: (model, rebrowse)
    _conf = {
        'check_object_reference': (True, False),
        'create': (True, True),
        'exists': (False, True),
        'fields_get': (True, False),
        'name_search': (True, False),
        'search': (True, True),
        'search_count': (True, False),
        'search_read': (True, False),
    }

    def browse(self, ids):
        return Model(self._env, self._model, ids)

    # because sorted is not xmlrpc-compatible (it doesn't downgrade properly)
    def sorted(self, field):
        fn = field if callable(field) else lambda r: r[field]

        return Model(self._env, self._model, (
            id
            for record in sorted(self, key=fn)
            for id in record.ids
        ))

    def __getitem__(self, index):
        if isinstance(index, str):
            return getattr(self, index)
        ids = self._ids[index]
        if isinstance(ids, int):
            ids = [ids]

        return Model(self._env, self._model, ids, fields=self._fields)

    def __getattr__(self, fieldname):
        if fieldname in ['__dataclass_fields__', '__attrs_attrs__']:
            raise AttributeError(f'{fieldname!r} is invalid on {self._model}')

        field_description = self._fields.get(fieldname)
        if field_description is None:
            return functools.partial(self._call, fieldname)

        if not self._ids:
            return False

        if fieldname == 'id':
            return self._ids[0]

        val = self.read([fieldname])[0][fieldname]
        field_description = self._fields[fieldname]
        if field_description['type'] in ('many2one', 'one2many', 'many2many'):
            val = val or []
            if field_description['type'] == 'many2one':
                val = val[:1] # (id, name) => [id]
            return Model(self._env, field_description['relation'], val)

        return val

    # because it's difficult to discriminate between methods and fields
    def _call(self, name, *args, **kwargs):
        model, rebrowse = self._conf.get(name, (False, False))

        if model:
            res = self._env(self._model, name, *args, **kwargs)
        else:
            res = self._env(self._model, name, self._ids, *args, **kwargs)

        if not rebrowse:
            return res
        if isinstance(res, int):
            return self.browse([res])
        return self.browse(res)

    def __setattr__(self, fieldname, value):
        self._env(self._model, 'write', self._ids, {fieldname: value})

    def __contains__(self, item: str | int) -> bool:
        if isinstance(item, str):
            return item in self._fields
        return item in self.ids

    def __iter__(self):
        return (
            Model(self._env, self._model, [i], fields=self._fields)
            for i in self._ids
        )

    def mapped(self, path):
        if callable(path):
            return [path(r) for r in self]

        field, *rest = path.split('.', 1)
        descr = self._fields[field]
        if descr['type'] in ('many2one', 'one2many', 'many2many'):
            result = Model(self._env, descr['relation'])
            for record in self:
                result |= getattr(record, field)

            return result.mapped(rest[0]) if rest else result

        assert not rest
        return [getattr(r, field) for r in self]

    def filtered(self, fn):
        result = Model(self._env, self._model, fields=self._fields)
        for record in self:
            if fn(record):
                result |= record
        return result

    def __sub__(self, other):
        if not isinstance(other, Model) or self._model != other._model:
            return NotImplemented

        return Model(self._env, self._model, tuple(id_ for id_ in self._ids if id_ not in other._ids), fields=self._fields)

    def __or__(self, other):
        if not isinstance(other, Model) or self._model != other._model:
            return NotImplemented

        return Model(
            self._env, self._model,
            self._ids + tuple(id_ for id_ in other.ids if id_ not in self._ids),
            fields=self._fields
        )
    __add__ = __or__

    def __and__(self, other):
        if not isinstance(other, Model) or self._model != other._model:
            return NotImplemented

        return Model(self._env, self._model, tuple(id_ for id_ in self._ids if id_ in other._ids), fields=self._fields)
