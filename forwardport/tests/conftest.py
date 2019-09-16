# -*- coding: utf-8 -*-
import base64
import collections
import copy
import itertools
import logging
import pathlib
import socket
import time
import uuid
import xmlrpc.client
from contextlib import closing

import pytest
import subprocess

import re
import requests
from shutil import rmtree

from odoo.tools.appdirs import user_cache_dir

DEFAULT_CRONS = [
    'runbot_merge.process_updated_commits',
    'runbot_merge.merge_cron',
    'forwardport.port_forward',
    'forwardport.updates',
    'runbot_merge.check_linked_prs_status',
    'runbot_merge.feedback_cron',
]


def pytest_report_header(config):
    return 'Running against database ' + config.getoption('--db')

def pytest_addoption(parser):
    parser.addoption('--db', help="DB to run the tests against", default=str(uuid.uuid4()))
    parser.addoption('--addons-path')
    parser.addoption("--no-delete", action="store_true", help="Don't delete repo after a failed run")

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

# public_repo — necessary to leave comments
# admin:repo_hook — to set up hooks (duh)
# delete_repo — to cleanup repos created under a user
# user:email — fetch token/user's email addresses
TOKEN_SCOPES = {
    'github': {'admin:repo_hook', 'delete_repo', 'public_repo', 'user:email'},
    # TODO: user:email so they can fetch the user's email?
    'role_reviewer': {'public_repo'},# 'delete_repo'},
    'role_self_reviewer': {'public_repo'},# 'delete_repo'},
    'role_other': {'public_repo'},# 'delete_repo'},
}
@pytest.fixture(autouse=True, scope='session')
def _check_scopes(config):
    for section, vals in config.items():
        required_scopes = TOKEN_SCOPES.get(section)
        if required_scopes is None:
            continue

        response = requests.get('https://api.github.com/rate_limit', headers={
            'Authorization': 'token %s' % vals['token']
        })
        assert response.status_code == 200
        x_oauth_scopes = response.headers['X-OAuth-Scopes']
        token_scopes = set(re.split(r',\s+', x_oauth_scopes))
        assert token_scopes >= required_scopes, \
            "%s should have scopes %s, found %s" % (section, token_scopes, required_scopes)

@pytest.fixture(autouse=True)
def _cleanup_cache(config, users):
    """ forwardport has a repo cache which it assumes is unique per name
    but tests always use the same repo paths / names for different repos
    (the repos get re-created), leading to divergent repo histories.

    So clear cache after each test, two tests should not share repos.
    """
    yield
    cache_root = pathlib.Path(user_cache_dir('forwardport'))
    rmtree(cache_root / config['github']['owner'], ignore_errors=True)
    for login in users.values():
        rmtree(cache_root / login, ignore_errors=True)

@pytest.fixture(autouse=True)
def users(users_):
    return users_

@pytest.fixture
def project(env, config):
    return env['runbot_merge.project'].create({
        'name': 'odoo',
        'github_token': config['github']['token'],
        'github_prefix': 'hansen',
        'fp_github_token': config['github']['token'],
        'required_statuses': 'legal/cla,ci/runbot',
    })

@pytest.fixture(scope='session')
def module():
    """ When a test function is (going to be) run, selects the containing
    module (as needing to be installed)
    """
    # NOTE: no request.fspath (because no request.function) in session-scoped fixture so can't put module() at the toplevel
    return 'forwardport'

@pytest.fixture(scope='session')
def port():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(('', 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]

@pytest.fixture(scope='session')
def dbcache(request, module):
    """ Creates template DB once per run, then just duplicates it before
    starting odoo and running the testcase
    """
    db = request.config.getoption('--db')
    subprocess.run([
        'odoo', '--no-http',
        '--addons-path', request.config.getoption('--addons-path'),
        '-d', db, '-i', module,
        '--max-cron-threads', '0',
        '--stop-after-init'
    ], check=True)
    yield db
    subprocess.run(['dropdb', db])

@pytest.fixture
def db(request, dbcache):
    rundb = str(uuid.uuid4())
    subprocess.run(['createdb', '-T', dbcache, rundb], check=True)

    yield rundb

    if not request.config.getoption('--no-delete'):
        subprocess.run(['dropdb', rundb], check=True)

@pytest.fixture
def server(request, db, port, module):
    p = subprocess.Popen([
        'odoo', '--http-port', str(port),
        '--addons-path', request.config.getoption('--addons-path'),
        '-d', db,
        '--max-cron-threads', '0', # disable cron threads (we're running crons by hand)
    ])

    try:
        wait_for_server(db, port, p, module)

        yield p
    finally:
        p.terminate()
        p.wait(timeout=30)

@pytest.fixture
def env(port, server, db):
    yield Environment(port, db)

# users is just so I can avoid autouse on toplevel users fixture b/c it (seems
# to) break the existing local tests
@pytest.fixture
def make_repo(request, config, tunnel, users):
    owner = config['github']['owner']
    github = requests.Session()
    github.headers['Authorization'] = 'token %s' % config['github']['token']

    # check whether "owner" is a user or an org, as repo-creation endpoint is
    # different
    q = github.get('https://api.github.com/users/{}'.format(owner))
    q.raise_for_status()
    if q.json().get('type') == 'Organization':
        endpoint = 'https://api.github.com/orgs/{}/repos'.format(owner)
    else:
        endpoint = 'https://api.github.com/user/repos'
        r = github.get('https://api.github.com/user')
        r.raise_for_status()
        assert r.json()['login'] == owner

    repos = []
    def repomaker(name):
        fullname = '{}/{}'.format(owner, name)
        repo_url = 'https://api.github.com/repos/{}'.format(fullname)
        if request.config.getoption('--no-delete'):
            if github.head(repo_url).ok:
                pytest.skip("Repository {} already exists".format(fullname))
        else:
            # just try to delete the repo, we don't really care
            if github.delete(repo_url).ok:
                # if we did delete a repo, wait a bit as gh might need to
                # propagate the thing?
                time.sleep(30)

        # create repo
        r = github.post(endpoint, json={
            'name': name,
            'has_issues': False,
            'has_projects': False,
            'has_wiki': False,
            'auto_init': False,
            # at least one merge method must be enabled :(
            'allow_squash_merge': False,
            # 'allow_merge_commit': False,
            'allow_rebase_merge': False,
        })
        r.raise_for_status()

        new_repo = Repo(github, fullname, repos)
        # create webhook
        github.post('{}/hooks'.format(repo_url), json={
            'name': 'web',
            'config': {
                'url': '{}/runbot_merge/hooks'.format(tunnel),
                'content_type': 'json',
                'insecure_ssl': '1',
            },
            'events': ['pull_request', 'issue_comment', 'status', 'pull_request_review']
        })

        github.put('https://api.github.com/repos/{}/contents/{}'.format(fullname, 'a'), json={
            'path': 'a',
            'message': 'github returns a 409 (Git Repository is Empty) if trying to create a tree in a repo with no objects',
            'content': base64.b64encode(b'whee').decode('ascii'),
            'branch': 'garbage_%s' % uuid.uuid4()
        }).raise_for_status()

        return new_repo

    yield repomaker

    if not request.config.getoption('--no-delete'):
        for repo in reversed(repos):
            repo.delete()

Commit = collections.namedtuple('Commit', 'id tree message author committer parents')
class Repo:
    def __init__(self, session, fullname, repos):
        self._session = session
        self.name = fullname
        self._repos = repos
        self.hook = False
        repos.append(self)

        # unwatch repo
        self.unsubscribe()

    def unsubscribe(self, token=None):
        self._get_session(token).put('https://api.github.com/repos/{}/subscription'.format(self.name), json={
            'subscribed': False,
            'ignored': True,
        })

    def delete(self):
        r = self._session.delete('https://api.github.com/repos/{}'.format(self.name))
        if r.status_code != 204:
            logging.getLogger(__name__).warn("Unable to delete repository %s", self.name)

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
            self.update_ref(name, commit, force=True)
            return
        assert 200 <= r.status_code < 300, r.json()

    def update_ref(self, name, commit, force=False):
        r = self._session.patch('https://api.github.com/repos/{}/git/refs/{}'.format(self.name, name), json={'sha': commit, 'force': force})
        assert 200 <= r.status_code < 300, r.json()

    def make_commits(self, root, *commits, ref=None):
        assert self.hook
        if root:
            c = self.commit(root)
            tree = c.tree
            parents = [c.id]
        else:
            tree = None
            parents = []

        hashes = []
        for commit in commits:
            if commit.reset:
                tree = None
            r = self._session.post('https://api.github.com/repos/{}/git/trees'.format(self.name), json={
                'tree': [
                    {'path': k, 'mode': '100644', 'type': 'blob', 'content': v}
                    for k, v in commit.tree.items()
                ],
                'base_tree': tree
            })
            assert 200 <= r.status_code < 300, r.json()
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
            assert 200 <= r.status_code < 300, r.json()

            hashes.append(r.json()['sha'])
            parents = [hashes[-1]]

        if ref:
            self.make_ref(ref, hashes[-1], force=True)

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

    def _get_session(self, token):
        s = self._session
        if token:
            s = requests.Session()
            s.headers['Authorization'] = 'token %s' % token
        return s

    def get_pr(self, number):
        # ensure PR exists before returning it
        self._session.head('https://api.github.com/repos/{}/pulls/{}'.format(
            self.name,
            number,
        )).raise_for_status()
        return PR(self, number)

    def make_pr(self, *, title=None, body=None, target, head, token=None):
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

        r = self._session.post(
            'https://api.github.com/repos/{}/pulls'.format(self.name),
            json={
                'title': title,
                'body': body,
                'head': head,
                'base': target,
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

    def __enter__(self):
        self.hook = 1
        return self
    def __exit__(self, *args):
        wait_for_hook(self.hook)
        self.hook = 0

class PR:
    __slots__ = ['number', 'repo']

    def __init__(self, repo, number):
        self.repo = repo
        self.number = number

    @property
    def _pr(self):
        r = self.repo._session.get('https://api.github.com/repos/{}/pulls/{}'.format(self.repo.name, self.number))
        assert 200 <= r.status_code < 300, r.json()
        return r.json()

    @property
    def head(self):
        return self._pr['head']['sha']

    @property
    def comments(self):
        r = self.repo._session.get('https://api.github.com/repos/{}/issues/{}/comments'.format(self.repo.name, self.number))
        assert 200 <= r.status_code < 300, r.json()
        return [
            (c['user']['login'], c['body'])
            for c in r.json()
        ]

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

    def _set_prop(self, prop, value):
        assert self.repo.hook
        r = self.repo._session.patch('https://api.github.com/repos/{}/pulls/{}'.format(self.repo.name, self.number), json={
            prop: value
        })
        assert 200 <= r.status_code < 300, r.json()

    def open(self):
        self._set_prop('state', 'open')

    def close(self):
        self._set_prop('state', 'closed')

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
PRBranch = collections.namedtuple('PRBranch', 'repo branch')

class Environment:
    def __init__(self, port, db):
        self._uid = xmlrpc.client.ServerProxy('http://localhost:{}/xmlrpc/2/common'.format(port)).authenticate(db, 'admin', 'admin', {})
        self._object = xmlrpc.client.ServerProxy('http://localhost:{}/xmlrpc/2/object'.format(port))
        self._db = db

    def __call__(self, model, method, *args, **kwargs):
        return self._object.execute_kw(
            self._db, self._uid, 'admin',
            model, method,
            args, kwargs
        )

    def __getitem__(self, name):
        return Model(self, name)

    def run_crons(self, *xids, **kw):
        crons = xids or DEFAULT_CRONS
        for xid in crons:
            _, model, cron_id = self('ir.model.data', 'xmlid_lookup', xid)
            assert model == 'ir.cron', "Expected {} to be a cron, got {}".format(xid, model)
            self('ir.cron', 'method_direct_trigger', [cron_id], **kw)
        # sleep for some time as a lot of crap may have happened (?)
        wait_for_hook()

class Model:
    __slots__ = ['_env', '_model', '_ids', '_fields']
    def __init__(self, env, model, ids=(), fields=None):
        object.__setattr__(self, '_env', env)
        object.__setattr__(self, '_model', model)
        object.__setattr__(self, '_ids', tuple(ids or ()))

        object.__setattr__(self, '_fields', fields or self._env(self._model, 'fields_get', attributes=['type', 'relation']))

    @property
    def ids(self):
        return self._ids

    def __bool__(self):
        return bool(self._ids)

    def __len__(self):
        return len(self._ids)

    def __eq__(self, other):
        if not isinstance(other, Model):
            return NotImplemented
        return self._model == other._model and self._ids == other._ids

    def __repr__(self):
        return "{}({})".format(self._model, ', '.join(str(id) for id in self._ids))

    def exists(self):
        ids = self._env(self._model, 'exists', self._ids)
        return Model(self._env, self._model, ids)

    def search(self, *args, **kwargs):
        ids = self._env(self._model, 'search', *args, **kwargs)
        return Model(self._env, self._model, ids)

    def create(self, values):
        return Model(self._env, self._model, [self._env(self._model, 'create', values)])

    def write(self, values):
        return self._env(self._model, 'write', self._ids, values)

    def read(self, fields):
        return self._env(self._model, 'read', self._ids, fields)

    def unlink(self):
        return self._env(self._model, 'unlink', self._ids)

    def __getitem__(self, index):
        if isinstance(index, str):
            return getattr(self, index)
        ids = self._ids[index]
        if isinstance(ids, int):
            ids = [ids]

        return Model(self._env, self._model, ids, fields=self._fields)

    def __getattr__(self, fieldname):
        if not self._ids:
            return False

        assert len(self._ids) == 1
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

    def __setattr__(self, fieldname, value):
        assert self._fields[fieldname]['type'] not in ('many2one', 'one2many', 'many2many')
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

        return Model(self._env, self._model, {*self._ids, *other._ids}, fields=self._fields)
    __add__ = __or__

    def __and__(self, other):
        if not isinstance(other, Model) or self._model != other._model:
            return NotImplemented

        return Model(self._env, self._model, tuple(id_ for id_ in self._ids if id_ in other._ids), fields=self._fields)


    def invalidate_cache(self, fnames=None, ids=None):
        pass # not a concern when every access is an RPC call
