# -*- coding: utf-8 -*-
"""
Configuration:

* an ``odoo`` binary in the path, which runs the relevant odoo; to ensure a
  clean slate odoo is re-started and a new database is created before each
  test (technically a "template" db is created first, then that DB is cloned
  and the fresh clone is used for each test)

* pytest.ini (at the root of the runbot repo or higher) with the following
  sections and keys

  ``github``
    - owner, the name of the account (personal or org) under which test repos
      will be created & deleted (note: some repos might be created under role
      accounts as well)
    - token, either personal or oauth, must have the scopes ``public_repo``,
      ``delete_repo`` and ``admin:repo_hook``, if personal the owner must be
      the corresponding user account, not an org. Also user:email for the
      forwardport / forwardbot tests

  ``role_reviewer``, ``role_self_reviewer`` and ``role_other``
    - name (optional, used as partner name when creating that, otherwise github
      login gets used)
    - email (optional, used as partner email when creating that, otherwise
      github email gets used, reviewer and self-reviewer must have an email)
    - token, a personal access token with the ``public_repo`` scope (otherwise
      the API can't leave comments), maybe eventually delete_repo (for personal
      forks)

    .. warning:: the accounts must *not* be flagged, or the webhooks on
                 commenting or creating reviews will not trigger, and the
                 tests will fail

* either ``ngrok`` or ``lt`` (localtunnel) available on the path. ngrok with
  a configured account is recommended: ngrok is more reliable than localtunnel
  but a free account is necessary to get a high-enough rate limiting for some
  of the multi-repo tests to work

Finally the tests aren't 100% reliable as they rely on quite a bit of network
traffic, it's possible that the tests fail due to network issues rather than
logic errors.
"""
import base64
import collections
import configparser
import contextlib
import copy
import functools
import http.client
import itertools
import os
import random
import re
import socket
import subprocess
import sys
import time
import uuid
import warnings
import xmlrpc.client
from contextlib import closing

import psutil
import pytest
import requests

NGROK_CLI = [
    'ngrok', 'start', '--none', '--region', 'eu',
]

def pytest_addoption(parser):
    parser.addoption('--addons-path')
    parser.addoption("--no-delete", action="store_true", help="Don't delete repo after a failed run")
    parser.addoption('--log-github', action='store_true')

    parser.addoption(
        '--tunnel', action="store", type="choice", choices=['ngrok', 'localtunnel'], default='ngrok',
        help="Which tunneling method to use to expose the local Odoo server "
             "to hook up github's webhook. ngrok is more reliable, but "
             "creating a free account is necessary to avoid rate-limiting "
             "issues (anonymous limiting is rate-limited at 20 incoming "
             "queries per minute, free is 40, multi-repo batching tests will "
             "blow through the former); localtunnel has no rate-limiting but "
             "the servers are way less reliable")


# noinspection PyUnusedLocal
def pytest_configure(config):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'mergebot_test_utils'))
    print(sys.path)

@pytest.fixture(scope='session', autouse=True)
def _set_socket_timeout():
    """ Avoid unlimited wait on standard sockets during tests, this is mostly
    an issue for non-trivial cron calls
    """
    socket.setdefaulttimeout(120.0)

@pytest.fixture(scope="session")
def config(pytestconfig):
    """ Flat version of the pytest config file (pytest.ini), parses to a
    simple dict of {section: {key: value}}

    """
    conf = configparser.ConfigParser(interpolation=None)
    conf.read([pytestconfig.inifile])
    cnf = {
        name: dict(s.items())
        for name, s in conf.items()
    }
    # special case user / owner / ...
    cnf['role_user'] = {
        'token': conf['github']['token']
    }
    return cnf

@pytest.fixture(scope='session')
def rolemap(request, config):
    # hack because capsys is not session-scoped
    capmanager = request.config.pluginmanager.getplugin("capturemanager")
    # only fetch github logins once per session
    rolemap = {}
    for k, data in config.items():
        if k.startswith('role_'):
            role = k[5:]
        elif k == 'github':
            role = 'user'
        else:
            continue

        with capmanager.global_and_fixture_disabled():
            r = _rate_limited(lambda: requests.get('https://api.github.com/user', headers={'Authorization': 'token %s' % data['token']}))
        r.raise_for_status()

        user = rolemap[role] = r.json()
        data['user'] = user['login']
    return rolemap

@pytest.fixture
def partners(env, config, rolemap):
    m = {}
    for role, u in rolemap.items():
        if role in ('user', 'other'):
            continue

        login = u['login']
        conf = config['role_' + role]
        m[role] = env['res.partner'].create({
            'name': conf.get('name', login),
            'email': conf.get('email') or u['email'] or False,
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
def users(partners, rolemap):
    return {k: v['login'] for k, v in rolemap.items()}

@pytest.fixture(scope='session')
def tunnel(pytestconfig, port):
    """ Creates a tunnel to localhost:<port> using ngrok or localtunnel, should yield the
    publicly routable address & terminate the process at the end of the session
    """
    tunnel = pytestconfig.getoption('--tunnel')
    if tunnel == 'ngrok':
        web_addr = 'http://localhost:4040/api'
        addr = 'localhost:%d' % port
        # try to find out if ngrok is running, and if it's not attempt
        # to start it
        try:
            # FIXME: this is for xdist to avoid workers running ngrok at the
            #        exact same time, use lockfile instead
            time.sleep(random.SystemRandom().randint(1, 10))
            # FIXME: use config file so we can set web_addr to something else
            #        than localhost:4040 (otherwise we can't disambiguate
            #        between the ngrok we started and an ngrok started by
            #        some other user)
            requests.get(web_addr)
        except requests.exceptions.ConnectionError:
            subprocess.Popen(NGROK_CLI, stdout=subprocess.DEVNULL)
            for _ in range(5):
                time.sleep(1)
                with contextlib.suppress(requests.exceptions.ConnectionError):
                    requests.get(web_addr)
                    break
            else:
                raise Exception("Unable to connect to ngrok")

        requests.post(f'{web_addr}/tunnels', json={
            'name': str(port),
            'proto': 'http',
            'bind_tls': True, # only https
            'addr': addr,
            'inspect': True,
        }).raise_for_status()

        tunnel = f'{web_addr}/tunnels/{port}'
        for _ in range(10):
            time.sleep(2)
            r = requests.get(tunnel)
            # not created yet, wait and retry
            if r.status_code == 404:
                continue
            # check for weird responses
            r.raise_for_status()
            try:
                yield r.json()['public_url']
            finally:
                requests.delete(tunnel)
                for _ in range(10):
                    time.sleep(1)
                    r = requests.get(tunnel)
                    # check if deletion is done
                    if r.status_code == 404:
                        break
                    r.raise_for_status()
                else:
                    raise TimeoutError("ngrok tunnel deletion failed")

                r = requests.get(f'{web_addr}/tunnels')
                # there are still tunnels in the list -> bail
                if r.ok and r.json()['tunnels']:
                    return

                # ngrok is broken or all tunnels have been shut down -> try to
                # find and kill it (but only if it looks a lot like we started it)
                for p in psutil.process_iter():
                    if p.name() == 'ngrok' and p.cmdline() == NGROK_CLI:
                        p.terminate()
                        break
                return
        else:
            raise TimeoutError("ngrok tunnel creation failed (?)")
    elif tunnel == 'localtunnel':
        p = subprocess.Popen(['lt', '-p', str(port)], stdout=subprocess.PIPE)
        try:
            r = p.stdout.readline()
            m = re.match(br'your url is: (https://.*\.localtunnel\.me)', r)
            assert m, "could not get the localtunnel URL"
            yield m.group(1).decode('ascii')
        finally:
            p.terminate()
            p.wait(30)
    else:
        raise ValueError("Unsupported %s tunnel method" % tunnel)

class DbDict(dict):
    def __init__(self, adpath):
        super().__init__()
        self._adpath = adpath
    def __missing__(self, module):
        self[module] = db = 'template_%s' % uuid.uuid4()
        subprocess.run([
            'odoo', '--no-http',
            '--addons-path', self._adpath,
            '-d', db, '-i', module,
            '--max-cron-threads', '0',
            '--stop-after-init',
            '--log-level', 'warn'
        ], check=True)
        return db

@pytest.fixture(scope='session')
def dbcache(request):
    """ Creates template DB once per run, then just duplicates it before
    starting odoo and running the testcase
    """
    dbs = DbDict(request.config.getoption('--addons-path'))
    yield dbs
    for db in dbs.values():
        subprocess.run(['dropdb', db], check=True)

@pytest.fixture
def db(request, module, dbcache):
    rundb = str(uuid.uuid4())
    subprocess.run(['createdb', '-T', dbcache[module], rundb], check=True)

    yield rundb

    if not request.config.getoption('--no-delete'):
        subprocess.run(['dropdb', rundb], check=True)

def wait_for_hook(n=1):
    time.sleep(10 * n)

def wait_for_server(db, port, proc, mod, timeout=120):
    """ Polls for server to be response & have installed our module.

    Raises socket.timeout on failure
    """
    limit = time.time() + timeout
    while True:
        if proc.poll() is not None:
            raise Exception("Server unexpectedly closed")

        try:
            uid = xmlrpc.client.ServerProxy(
                'http://localhost:{}/xmlrpc/2/common'.format(port))\
                .authenticate(db, 'admin', 'admin', {})
            mods = xmlrpc.client.ServerProxy(
                'http://localhost:{}/xmlrpc/2/object'.format(port))\
                .execute_kw(
                    db, uid, 'admin', 'ir.module.module', 'search_read', [
                    [('name', '=', mod)], ['state']
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
def server(request, db, port, module):
    log_handlers = [
        'odoo.modules.loading:WARNING',
    ]
    if not request.config.getoption('--log-github'):
        log_handlers.append('github_requests:WARNING')

    p = subprocess.Popen([
        'odoo', '--http-port', str(port),
        '--addons-path', request.config.getoption('--addons-path'),
        '-d', db,
        '--max-cron-threads', '0', # disable cron threads (we're running crons by hand)
        *itertools.chain.from_iterable(('--log-handler', h) for h in log_handlers),
    ])

    try:
        wait_for_server(db, port, p, module)

        yield p
    finally:
        p.terminate()
        p.wait(timeout=30)

@pytest.fixture
def env(port, server, db, default_crons):
    yield Environment(port, db, default_crons)

def check(response):
    assert response.ok, response.text or response.reason
    return response
# users is just so I can avoid autouse on toplevel users fixture b/c it (seems
# to) break the existing local tests
@pytest.fixture
def make_repo(capsys, request, config, tunnel, users):
    owner = config['github']['owner']
    github = requests.Session()
    github.headers['Authorization'] = 'token %s' % config['github']['token']

    # check whether "owner" is a user or an org, as repo-creation endpoint is
    # different
    with capsys.disabled():
        q = _rate_limited(lambda: github.get('https://api.github.com/users/{}'.format(owner)))
    q.raise_for_status()
    if q.json().get('type') == 'Organization':
        endpoint = 'https://api.github.com/orgs/{}/repos'.format(owner)
    else:
        endpoint = 'https://api.github.com/user/repos'
        r = check(github.get('https://api.github.com/user'))
        assert r.json()['login'] == owner

    repos = []
    def repomaker(name):
        name = 'ignore_%s_%s' % (name, base64.b64encode(os.urandom(6), b'-_').decode())
        fullname = '{}/{}'.format(owner, name)
        repo_url = 'https://api.github.com/repos/{}'.format(fullname)

        # create repo
        r = check(github.post(endpoint, json={
            'name': name,
            'has_issues': False,
            'has_projects': False,
            'has_wiki': False,
            'auto_init': False,
            # at least one merge method must be enabled :(
            'allow_squash_merge': False,
            # 'allow_merge_commit': False,
            'allow_rebase_merge': False,
        }))
        r = r.json()
        # wait for repository visibility
        while True:
            time.sleep(1)
            if github.head(r['url']).ok:
                break

        repo = Repo(github, fullname, repos)

        # create webhook
        check(github.post('{}/hooks'.format(repo_url), json={
            'name': 'web',
            'config': {
                'url': '{}/runbot_merge/hooks'.format(tunnel),
                'content_type': 'json',
                'insecure_ssl': '1',
            },
            'events': ['pull_request', 'issue_comment', 'status', 'pull_request_review']
        }))
        time.sleep(1)

        check(github.put('{}/contents/{}'.format(repo_url, 'a'), json={
            'path': 'a',
            'message': 'github returns a 409 (Git Repository is Empty) if trying to create a tree in a repo with no objects',
            'content': base64.b64encode(b'whee').decode('ascii'),
            'branch': 'garbage_%s' % uuid.uuid4()
        }))
        time.sleep(1)
        return repo

    yield repomaker

    if not request.config.getoption('--no-delete'):
        for repo in reversed(repos):
            repo.delete()


def _rate_limited(req):
    while True:
        q = req()
        if not q.ok and q.headers.get('X-RateLimit-Remaining') == '0':
            reset = int(q.headers['X-RateLimit-Reset'])
            delay = max(0, round(reset - time.time() + 1.0))
            print("Hit rate limit, sleeping for", delay, "seconds")
            time.sleep(delay)
            continue
        break
    return q


Commit = collections.namedtuple('Commit', 'id tree message author committer parents')
class Repo:
    def __init__(self, session, fullname, repos):
        self._session = session
        self.name = fullname
        self._repos = repos
        self.hook = False
        repos.append(self)

    @property
    def owner(self):
        return self.name.split('/')[0]

    def unsubscribe(self, token=None):
        self._get_session(token).put('https://api.github.com/repos/{}/subscription'.format(self.name), json={
            'subscribed': False,
            'ignored': True,
        })

    def add_collaborator(self, login, token):
        # send invitation to user
        r = check(self._session.put('https://api.github.com/repos/{}/collaborators/{}'.format(self.name, login)))
        # accept invitation on behalf of user
        check(requests.patch('https://api.github.com/user/repository_invitations/{}'.format(r.json()['id']), headers={
            'Authorization': 'token ' + token
        }))
        # sanity check that user is part of collaborators
        r = check(self._session.get('https://api.github.com/repos/{}/collaborators'.format(self.name)))
        assert any(login == c['login'] for c in r.json())

    def _get_session(self, token):
        s = self._session
        if token:
            s = requests.Session()
            s.headers['Authorization'] = 'token %s' % token
        return s

    def delete(self):
        r = self._session.delete('https://api.github.com/repos/{}'.format(self.name))
        if r.status_code != 204:
            warnings.warn("Unable to delete repository %s (HTTP %s)" % (self.name, r.status_code))

    def set_secret(self, secret):
        assert self.hook
        r = self._session.get(
            'https://api.github.com/repos/{}/hooks'.format(self.name))
        response = r.json()
        assert 200 <= r.status_code < 300, response
        [hook] = response

        r = self._session.patch('https://api.github.com/repos/{}/hooks/{}'.format(self.name, hook['id']), json={
            'config': {**hook['config'], 'secret': secret},
        })
        assert 200 <= r.status_code < 300, r.json()

    def get_ref(self, ref):
        # differs from .commit(ref).id for the sake of assertion error messages
        # apparently commits/{ref} returns 422 or some other fool thing when the
        # ref' does not exist which sucks for asserting "the ref' has been
        # deleted"
        # FIXME: avoid calling get_ref on a hash & remove this code
        if re.match(r'[0-9a-f]{40}', ref):
            # just check that the commit exists
            r = self._session.get('https://api.github.com/repos/{}/git/commits/{}'.format(self.name, ref))
            assert 200 <= r.status_code < 300, r.reason or http.client.responses[r.status_code]
            return r.json()['sha']

        if ref.startswith('refs/'):
            ref = ref[5:]
        if not ref.startswith('heads'):
            ref = 'heads/' + ref

        r = self._session.get('https://api.github.com/repos/{}/git/ref/{}'.format(self.name, ref))
        assert 200 <= r.status_code < 300, r.reason or http.client.responses[r.status_code]
        res = r.json()
        assert res['object']['type'] == 'commit'
        return res['object']['sha']

    def commit(self, ref):
        if not re.match(r'[0-9a-f]{40}', ref):
            if not ref.startswith(('heads/', 'refs/heads/')):
                ref = 'refs/heads/' + ref
        # apparently heads/<branch> ~ refs/heads/<branch> but are not
        # necessarily up to date ??? unlike the git ref system where :ref
        # starts at heads/
        if ref.startswith('heads/'):
            ref = 'refs/' + ref

        r = self._session.get('https://api.github.com/repos/{}/commits/{}'.format(self.name, ref))
        response = r.json()
        assert 200 <= r.status_code < 300, response

        return self._commit_from_gh(response)

    def _commit_from_gh(self, gh_commit):
        c = gh_commit['commit']
        return Commit(
            id=gh_commit['sha'],
            tree=c['tree']['sha'],
            message=c['message'],
            author=c['author'],
            committer=c['committer'],
            parents=[p['sha'] for p in gh_commit['parents']],
        )

    def log(self, ref_or_sha):
        for page in itertools.count(1):
            r = self._session.get(
                'https://api.github.com/repos/{}/commits'.format(self.name),
                params={'sha': ref_or_sha, 'page': page}
            )
            assert 200 <= r.status_code < 300, r.json()
            yield from map(self._commit_from_gh, r.json())
            if not r.links.get('next'):
                return

    def read_tree(self, commit):
        """ read tree object from commit

        :param Commit commit:
        :rtype: Dict[str, str]
        """
        r = self._session.get('https://api.github.com/repos/{}/git/trees/{}'.format(self.name, commit.tree))
        assert 200 <= r.status_code < 300, r.json()

        # read tree's blobs
        tree = {}
        for t in r.json()['tree']:
            assert t['type'] == 'blob', "we're *not* doing recursive trees in test cases"
            r = self._session.get('https://api.github.com/repos/{}/git/blobs/{}'.format(self.name, t['sha']))
            assert 200 <= r.status_code < 300, r.json()
            tree[t['path']] = base64.b64decode(r.json()['content']).decode()

        return tree

    def make_ref(self, name, commit, force=False):
        assert self.hook
        assert name.startswith('heads/')
        r = self._session.post('https://api.github.com/repos/{}/git/refs'.format(self.name), json={
            'ref': 'refs/' + name,
            'sha': commit,
        })
        if force and r.status_code == 422:
            self.update_ref(name, commit, force=force)
            return
        assert r.ok, r.text

    def update_ref(self, name, commit, force=False):
        assert self.hook
        r = self._session.patch('https://api.github.com/repos/{}/git/refs/{}'.format(self.name, name), json={'sha': commit, 'force': force})
        assert r.ok, r.text

    def protect(self, branch):
        assert self.hook
        r = self._session.put('https://api.github.com/repos/{}/branches/{}/protection'.format(self.name, branch), json={
            'required_status_checks': None,
            'enforce_admins': True,
            'required_pull_request_reviews': None,
            'restrictions': None,
        })
        assert 200 <= r.status_code < 300, r.json()

    # FIXME: remove this (runbot_merge should use make_commits directly)
    def make_commit(self, ref, message, author, committer=None, tree=None, wait=True):
        assert tree
        if isinstance(ref, list):
            assert all(re.match(r'[0-9a-f]{40}', r) for r in ref)
            ancestor_id = ref
            ref = None
        else:
            ancestor_id = self.get_ref(ref) if ref else None
            # if ref is already a commit id, don't pass it in
            if ancestor_id == ref:
                ref = None

        [h] = self.make_commits(
            ancestor_id,
            MakeCommit(message, tree=tree, author=author, committer=committer, reset=True),
            ref=ref
        )
        return h

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
                r = self._session.post('https://api.github.com/repos/{}/git/trees'.format(self.name), json={
                    'tree': [
                        {'path': k, 'mode': '100644', 'type': 'blob', 'content': v}
                        for k, v in commit.tree.items()
                    ],
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

            r = self._session.post('https://api.github.com/repos/{}/git/commits'.format(self.name), json=data)
            assert r.ok, r.text

            hashes.append(r.json()['sha'])
            parents = [hashes[-1]]

        if ref:
            fn = self.make_ref if make else self.update_ref
            fn(ref, hashes[-1], force=True)

        return hashes

    def fork(self, *, token=None):
        s = self._get_session(token)

        r = s.post('https://api.github.com/repos/{}/forks'.format(self.name))
        assert 200 <= r.status_code < 300, r.json()

        repo_name = r.json()['full_name']
        repo_url = 'https://api.github.com/repos/' + repo_name
        # poll for end of fork
        limit = time.time() + 60
        while s.head(repo_url, timeout=5).status_code != 200:
            if time.time() > limit:
                raise TimeoutError("No response for repo %s over 60s" % repo_name)
            time.sleep(1)

        return Repo(s, repo_name, self._repos)

    def get_pr(self, number):
        # ensure PR exists before returning it
        self._session.head('https://api.github.com/repos/{}/pulls/{}'.format(
            self.name,
            number,
        )).raise_for_status()
        return PR(self, number)

    def make_pr(self, *, title=None, body=None, target, head, draft=False, token=None):
        assert self.hook
        self.hook = 2

        if title is None:
            assert ":" not in head, \
                "will not auto-infer titles for PRs in a remote repo"
            c = self.commit(head)
            parts = iter(c.message.split('\n\n', 1))
            title = next(parts)
            body = next(parts, None)

        headers = {}
        if token:
            headers['Authorization'] = 'token {}'.format(token)

        # FIXME: change tests which pass a commit id to make_pr & remove this
        if re.match(r'[0-9a-f]{40}', head):
            ref = "temp_trash_because_head_must_be_a_ref_%d" % next(ct)
            self.make_ref('heads/' + ref, head)
            head = ref

        r = self._session.post(
            'https://api.github.com/repos/{}/pulls'.format(self.name),
            json={
                'title': title,
                'body': body,
                'head': head,
                'base': target,
                'draft': draft,
            },
            headers=headers,
        )
        pr = r.json()
        assert 200 <= r.status_code < 300, pr

        return PR(self, pr['number'])

    def post_status(self, ref, status, context='default', **kw):
        assert self.hook
        assert status in ('error', 'failure', 'pending', 'success')
        r = self._session.post('https://api.github.com/repos/{}/statuses/{}'.format(self.name, self.commit(ref).id), json={
            'state': status,
            'context': context,
            **kw
        })
        assert 200 <= r.status_code < 300, r.json()

    def is_ancestor(self, sha, of):
        return any(c['sha'] == sha for c in self.log(of))

    def log(self, ref_or_sha):
        for page in itertools.count(1):
            r = self._session.get(
                'https://api.github.com/repos/{}/commits'.format(self.name),
                params={'sha': ref_or_sha, 'page': page}
            )
            assert 200 <= r.status_code < 300, r.json()
            yield from r.json()
            if not r.links.get('next'):
                return

    def __enter__(self):
        self.hook = 1
        return self
    def __exit__(self, *args):
        wait_for_hook(self.hook)
        self.hook = 0
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
            'https://api.github.com/repos/{}/pulls/{}'.format(self.repo.name, self.number),
            headers=caching
        )
        assert r.ok, r.json()
        if r.status_code == 304:
            return previous
        contents, caching = self._cache = r.json(), {}
        if r.headers.get('etag'):
            caching['If-None-Match'] = r.headers['etag']
        if r.headers.get('last-modified'):
            caching['If-Modified-Since']= r.headers['Last-Modified']
        return contents

    @property
    def title(self):
        return self._pr['title']
    title = title.setter(lambda self, v: self._set_prop('title', v))

    @property
    def base(self):
        return self._pr['base']
    base = base.setter(lambda self, v: self._set_prop('base', v))

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
    def body(self):
        return self._pr['body']

    @property
    def comments(self):
        r = self.repo._session.get('https://api.github.com/repos/{}/issues/{}/comments'.format(self.repo.name, self.number))
        assert 200 <= r.status_code < 300, r.json()
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
            'https://api.github.com/repos/{}/issues/{}/comments'.format(self.repo.name, self.number),
            json={'body': body},
            headers=headers,
        )
        assert 200 <= r.status_code < 300, r.json()
        return r.json()['id']

    def edit_comment(self, cid, body, token=None):
        assert self.repo.hook
        headers = {}
        if token:
            headers['Authorization'] = 'token %s' % token
        r = self.repo._session.patch(
            'https://api.github.com/repos/{}/issues/comments/{}'.format(self.repo.name, cid),
            json={'body': body},
            headers=headers
        )
        assert 200 <= r.status_code < 300, r.json()
        wait_for_hook()

    def delete_comment(self, cid, token=None):
        assert self.repo.hook
        headers = {}
        if token:
            headers['Authorization'] = 'token %s' % token
        r = self.repo._session.delete(
            'https://api.github.com/repos/{}/issues/comments/{}'.format(self.repo.name, cid),
            headers=headers
        )
        assert r.status_code == 204, r.json()

    def _set_prop(self, prop, value, token=None):
        assert self.repo.hook
        headers = {}
        if token:
            headers['Authorization'] = 'token ' + token
        r = self.repo._session.patch('https://api.github.com/repos/{}/pulls/{}'.format(self.repo.name, self.number), json={
            prop: value
        }, headers=headers)
        assert r.ok, r.text

    def open(self, token=None):
        self._set_prop('state', 'open', token=token)

    def close(self, token=None):
        self._set_prop('state', 'closed', token=token)

    @property
    def branch(self):
        r = self.repo._session.get('https://api.github.com/repos/{}/pulls/{}'.format(
            self.repo.name,
            self.number,
        ))
        assert 200 <= r.status_code < 300, r.json()
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
            headers['Authorization'] = 'token %s' % token
        r = self.repo._session.post(
            'https://api.github.com/repos/{}/pulls/{}/reviews'.format(self.repo.name, self.number),
            json={'body': body, 'event': state,},
            headers=headers
        )
        assert 200 <= r.status_code < 300, r.json()

PRBranch = collections.namedtuple('PRBranch', 'repo branch')
class LabelsProxy(collections.abc.MutableSet):
    def __init__(self, pr):
        self._pr = pr

    @property
    def _labels(self):
        pr = self._pr
        r = pr.repo._session.get('https://api.github.com/repos/{}/issues/{}/labels'.format(pr.repo.name, pr.number))
        assert r.ok, r.json()
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
        r = pr.repo._session.post('https://api.github.com/repos/{}/issues/{}/labels'.format(pr.repo.name, pr.number), json={
            'labels': [label]
        })
        assert r.ok, r.json()

    def discard(self, label):
        pr = self._pr
        assert pr.repo.hook
        r = pr.repo._session.delete('https://api.github.com/repos/{}/issues/{}/labels/{}'.format(pr.repo.name, pr.number, label))
        # discard should do nothing if the item didn't exist in the set
        assert r.ok or r.status_code == 404, r.json()

    def update(self, *others):
        pr = self._pr
        assert pr.repo.hook
        # because of course that one is not provided by MutableMapping...
        r = pr.repo._session.post('https://api.github.com/repos/{}/issues/{}/labels'.format(pr.repo.name, pr.number), json={
            'labels': list(set(itertools.chain.from_iterable(others)))
        })
        assert r.ok, r.json()

class Environment:
    def __init__(self, port, db, default_crons=()):
        self._uid = xmlrpc.client.ServerProxy('http://localhost:{}/xmlrpc/2/common'.format(port)).authenticate(db, 'admin', 'admin', {})
        self._object = xmlrpc.client.ServerProxy('http://localhost:{}/xmlrpc/2/object'.format(port))
        self._db = db
        self._default_crons = default_crons

    def __call__(self, model, method, *args, **kwargs):
        return self._object.execute_kw(
            self._db, self._uid, 'admin',
            model, method,
            args, kwargs
        )

    def __getitem__(self, name):
        return Model(self, name)

    def run_crons(self, *xids, **kw):
        crons = xids or self._default_crons
        print('running crons', crons, file=sys.stderr)
        for xid in crons:
            t0 = time.time()
            print('\trunning cron', xid, '...', file=sys.stderr)
            _, model, cron_id = self('ir.model.data', 'xmlid_lookup', xid)
            assert model == 'ir.cron', "Expected {} to be a cron, got {}".format(xid, model)
            self('ir.cron', 'method_direct_trigger', [cron_id], **kw)
            print('\tdone %.3fs' % (time.time() - t0), file=sys.stderr)
        print('done', file=sys.stderr)
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

    def __eq__(self, other):
        if not isinstance(other, Model):
            return NotImplemented
        return self._model == other._model and set(self._ids) == set(other._ids)

    def __repr__(self):
        return "{}({})".format(self._model, ', '.join(str(id_) for id_ in self._ids))

    def browse(self, ids):
        return Model(self._env, self._model, ids)

    def exists(self):
        ids = self._env(self._model, 'exists', self._ids)
        return Model(self._env, self._model, ids)

    def search(self, *args, **kwargs):
        ids = self._env(self._model, 'search', *args, **kwargs)
        return Model(self._env, self._model, ids)

    def name_search(self, *args, **kwargs):
        return self._env(self._model, 'name_search', *args, **kwargs)

    def create(self, values):
        return Model(self._env, self._model, [self._env(self._model, 'create', values)])

    def write(self, values):
        return self._env(self._model, 'write', self._ids, values)

    def read(self, fields):
        return self._env(self._model, 'read', self._ids, fields)

    def name_get(self):
        return self._env(self._model, 'name_get', self._ids)

    def unlink(self):
        return self._env(self._model, 'unlink', self._ids)

    def sorted(self, field):
        rs = sorted(self.read([field]), key=lambda r: r[field])
        return Model(self._env, self._model, [r['id'] for r in rs])

    def __getitem__(self, index):
        if isinstance(index, str):
            return getattr(self, index)
        ids = self._ids[index]
        if isinstance(ids, int):
            ids = [ids]

        return Model(self._env, self._model, ids, fields=self._fields)

    def __getattr__(self, fieldname):
        if fieldname in ['__dataclass_fields__', '__attrs_attrs__']:
            raise AttributeError('%r is invalid on %s' % (fieldname, self._model))

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
        return self._env(self._model, name, self._ids, *args, **kwargs)

    def __setattr__(self, fieldname, value):
        self._env(self._model, 'write', self._ids, {fieldname: value})

    def __iter__(self):
        return (
            Model(self._env, self._model, [i], fields=self._fields)
            for i in self._ids
        )

    def mapped(self, path):
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

    def invalidate_cache(self, fnames=None, ids=None):
        pass # not a concern when every access is an RPC call
