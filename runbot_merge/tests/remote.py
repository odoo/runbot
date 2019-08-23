"""
Replaces relevant fixtures to allow running the test suite against github
actual (instead of a mocked version).

To enable this plugin, load it using ``-p runbot_merge.tests.remote``

.. WARNING:: this requires running ``python -mpytest`` from the root of the
             runbot repository, running ``pytest`` directly will not pick it
             up (as it does not setup ``sys.path``)

Configuration:

* an ``odoo`` binary in the path, which runs the relevant odoo; to ensure a
  clean slate odoo is re-started and a new database is created before each
  test

* pytest.ini (at the root of the runbot repo) with the following sections and
  keys

  ``github``
    - owner, the name of the account (personal or org) under which test repos
      will be created & deleted
    - token, either personal or oauth, must have the scopes ``public_repo``,
      ``delete_repo`` and ``admin:repo_hook``, if personal the owner must be
      the corresponding user account, not an org

  ``role_reviewer``, ``role_self_reviewer`` and ``role_other``
    - name (optional)
    - token, a personal access token with the ``public_repo`` scope (otherwise
      the API can't leave comments)

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
import itertools
import re
import socket
import subprocess
import time
import xmlrpc.client

import pytest
import requests

# Should be pytest_configure, but apparently once a plugin is registered
# its fixtures don't get unloaded even if it's unregistered, so prevent
# registering local entirely. This works because explicit plugins (-p)
# are loaded before conftest and conftest-specified plugins (officially:
# https://docs.pytest.org/en/latest/writing_plugins.html#plugin-discovery-order-at-tool-startup).

def pytest_addhooks(pluginmanager):
    pluginmanager.set_blocked('local')

def pytest_addoption(parser):
    parser.addoption("--no-delete", action="store_true", help="Don't delete repo after a failed run")


PORT=8069

@pytest.fixture(scope='session')
def port():
    return PORT

def wait_for_hook(n=1):
    # TODO: find better way to wait for roundtrip of actions which can trigger webhooks
    time.sleep(10 * n)

@pytest.fixture
def page():
    s = requests.Session()
    def get(url):
        r = s.get('http://localhost:{}{}'.format(PORT, url))
        r.raise_for_status()
        return r.content
    return get

def wait_for_server(db, timeout=120):
    """ Polls for server to be response & have installed our module.

    Raises socket.timeout on failure
    """
    limit = time.time() + timeout
    while True:
        try:
            uid = xmlrpc.client.ServerProxy(
                'http://localhost:{}/xmlrpc/2/common'.format(PORT))\
                .authenticate(db, 'admin', 'admin', {})
            xmlrpc.client.ServerProxy(
                'http://localhost:{}/xmlrpc/2/object'.format(PORT)) \
                .execute_kw(db, uid, 'admin', 'runbot_merge.batch', 'search',
                            [[]], {'limit': 1})
            break
        except ConnectionRefusedError:
            if time.time() > limit:
                raise socket.timeout()

@pytest.fixture(scope='session')
def remote_p():
    return True

@pytest.fixture
def env(request):
    """
    creates a db & an environment object as a proxy to xmlrpc calls
    """
    db = request.config.getoption('--db')
    p = subprocess.Popen([
        'odoo', '--http-port', str(PORT),
        '--addons-path', request.config.getoption('--addons-path'),
        '-d', db, '-i', 'runbot_merge',
        '--load', 'base,web,runbot_merge',
        '--max-cron-threads', '0', # disable cron threads (we're running crons by hand)
    ])

    try:
        wait_for_server(db)

        yield Environment(PORT, db)

        db_service = xmlrpc.client.ServerProxy('http://localhost:{}/xmlrpc/2/db'.format(PORT))
        db_service.drop('admin', db)
    finally:
        p.terminate()
        p.wait(timeout=30)

@pytest.fixture(autouse=True)
def users(users_):
    return users_

@pytest.fixture
def project(env, config):
    return env['runbot_merge.project'].create({
        'name': 'odoo',
        'github_token': config['github']['token'],
        'github_prefix': 'hansen',
        'branch_ids': [(0, 0, {'name': 'master'})],
        'required_statuses': 'legal/cla,ci/runbot',
    })

@pytest.fixture(scope='session')
def github(config):
    s = requests.Session()
    s.headers['Authorization'] = 'token {}'.format(config['github']['token'])
    return s

@pytest.fixture
def owner(config):
    return config['github']['owner']

@pytest.fixture
def make_repo(request, config, project, github, tunnel, users, owner):
    # check whether "owner" is a user or an org, as repo-creation endpoint is
    # different
    q = github.get('https://api.github.com/users/{}'.format(owner))
    q.raise_for_status()
    if q.json().get('type') == 'Organization':
        endpoint = 'https://api.github.com/orgs/{}/repos'.format(owner)
    else:
        # if not creating repos under an org, ensure the token matches the owner
        assert users['user'] == owner, "when testing against a user (rather than an organisation) the API token must be the user's"
        endpoint = 'https://api.github.com/user/repos'

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
        repos.append(fullname)
        # unwatch repo
        github.put('{}/subscription'.format(repo_url), json={
            'subscribed': False,
            'ignored': True,
        })
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
        project.write({'repo_ids': [(0, 0, {'name': fullname})]})

        role_tokens = {
            n[5:]: vals['token']
            for n, vals in config.items()
            if n.startswith('role_')
        }
        role_tokens['user'] = config['github']['token']

        return Repo(github, fullname, role_tokens)

    yield repomaker

    if not request.config.getoption('--no-delete'):
        for repo in reversed(repos):
            github.delete('https://api.github.com/repos/{}'.format(repo)).raise_for_status()

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

    def search(self, domain, **kw):
        ids = self._env(self._model, 'search', domain, **kw)
        return Model(self._env, self._model, ids)

    def create(self, values):
        return Model(self._env, self._model, [self._env(self._model, 'create', values)])

    def write(self, values):
        return self._env(self._model, 'write', self._ids, values)

    def read(self, fields):
        return self._env(self._model, 'read', self._ids, fields)

    def unlink(self):
        return self._env(self._model, 'unlink', self._ids)

    def _check_progress(self):
        assert self._model == 'runbot_merge.project'
        self._run_cron('runbot_merge.merge_cron')

    def _check_fetch(self):
        assert self._model == 'runbot_merge.project'
        self._run_cron('runbot_merge.fetch_prs_cron')

    def _send_feedback(self):
        assert self._model == 'runbot_merge.project'
        self._run_cron('runbot_merge.feedback_cron')

    def _check_linked_prs_statuses(self):
        assert self._model == 'runbot_merge.pull_requests'
        self._run_cron('runbot_merge.check_linked_prs_status')

    def _notify(self):
        assert self._model == 'runbot_merge.commit'
        self._run_cron('runbot_merge.process_updated_commits')

    def _run_cron(self, xid):
        _, model, cron_id = self._env('ir.model.data', 'xmlid_lookup', xid)
        assert model == 'ir.cron', "Expected {} to be a cron, got {}".format(xid, model)
        self._env('ir.cron', 'method_direct_trigger', [cron_id])
        # sleep for some time as a lot of crap may have happened (?)
        wait_for_hook()

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

    def __or__(self, other):
        if not isinstance(other, Model) or self._model != other._model:
            return NotImplemented

        return Model(self._env, self._model, {*self._ids, *other._ids}, fields=self._fields)

    def invalidate_cache(self, fnames=None, ids=None):
        pass # not a concern when every access is an RPC call

class Repo:
    __slots__ = ['name', '_session', '_tokens']
    def __init__(self, session, name, user_tokens):
        self.name = name
        self._session = session
        self._tokens = user_tokens

    def set_secret(self, secret):
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
        if re.match(r'[0-9a-f]{40}', ref):
            return ref

        assert ref.startswith('heads/')
        r = self._session.get('https://api.github.com/repos/{}/git/refs/{}'.format(self.name, ref))
        response = r.json()

        assert 200 <= r.status_code < 300, response
        assert isinstance(response, dict), "{} doesn't exist (got {} refs)".format(ref, len(response))
        assert response['object']['type'] == 'commit'

        return response['object']['sha']

    def make_ref(self, name, commit, force=False):
        assert name.startswith('heads/')
        r = self._session.post('https://api.github.com/repos/{}/git/refs'.format(self.name), json={
            'ref': 'refs/' + name,
            'sha': commit,
        })
        if force and r.status_code == 422:
            r = self._session.patch('https://api.github.com/repos/{}/git/refs/{}'.format(self.name, name), json={'sha': commit, 'force': True})
        assert 200 <= r.status_code < 300, r.json()
        wait_for_hook()

    def protect(self, branch):
        r = self._session.put('https://api.github.com/repos/{}/branches/{}/protection'.format(self.name, branch), json={
            'required_status_checks': None,
            'enforce_admins': True,
            'required_pull_request_reviews': None,
            'restrictions': None,
        })
        assert 200 <= r.status_code < 300, r.json()
        wait_for_hook()

    def update_ref(self, name, commit, force=False):
        r = self._session.patch('https://api.github.com/repos/{}/git/refs/{}'.format(self.name, name), json={'sha': commit, 'force': force})
        assert 200 <= r.status_code < 300, r.json()
        wait_for_hook()

    def make_commit(self, ref, message, author, committer=None, tree=None, wait=True):
        assert tree, "not supporting changes/updates"

        if not ref: # None / []
            # apparently github refuses to create trees/commits in empty repos
            # using the regular API...
            [(path, contents)] = tree.items()
            r = self._session.put('https://api.github.com/repos/{}/contents/{}'.format(self.name, path), json={
                'path': path,
                'message': message,
                'content': base64.b64encode(contents.encode('utf-8')).decode('ascii'),
                'branch': 'nootherwaytocreateaninitialcommitbutidontwantamasteryet%d' % next(ct)
            })
            assert 200 <= r.status_code < 300, r.json()
            return r.json()['commit']['sha']

        if isinstance(ref, list):
            refs = ref
        else:
            refs = [ref]
        parents = [self.get_ref(r) for r in refs]

        r = self._session.post('https://api.github.com/repos/{}/git/trees'.format(self.name), json={
            'tree': [
                {'path': k, 'mode': '100644', 'type': 'blob', 'content': v}
                for k, v in tree.items()
            ]
        })
        assert 200 <= r.status_code < 300, r.json()
        h = r.json()['sha']

        data = {
            'parents': parents,
            'message': message,
            'tree': h,
        }
        if author:
            data['author'] = author
        if committer:
            data['committer'] = committer

        r = self._session.post('https://api.github.com/repos/{}/git/commits'.format(self.name), json=data)
        assert 200 <= r.status_code < 300, r.json()

        commit_sha = r.json()['sha']

        # if the first parent is an actual ref (rather than a hash) update it
        if parents[0] != refs[0]:
            self.update_ref(refs[0], commit_sha)
        elif wait:
            wait_for_hook()
        return commit_sha

    def make_pr(self, title, body, target, ctid, user, label=None):
        # github only allows PRs from actual branches, so create an actual branch
        ref = label or "temp_trash_because_head_must_be_a_ref_%d" % next(ct)
        self.make_ref('heads/' + ref, ctid)

        r = self._session.post(
            'https://api.github.com/repos/{}/pulls'.format(self.name),
            json={'title': title, 'body': body, 'head': ref, 'base': target,},
            headers={'Authorization': 'token {}'.format(self._tokens[user])}
        )
        assert 200 <= r.status_code < 300, r.json()
        # wait extra for PRs creating many PRs and relying on their ordering
        # (test_batching & test_batching_split)
        # would be nice to make the tests more reliable but not quite sure
        # how...
        wait_for_hook(2)
        return PR(self, 'heads/' + ref, r.json()['number'])

    def post_status(self, ref, status, context='default', **kw):
        assert status in ('error', 'failure', 'pending', 'success')
        r = self._session.post('https://api.github.com/repos/{}/statuses/{}'.format(self.name, self.get_ref(ref)), json={
            'state': status,
            'context': context,
            **kw
        })
        assert 200 <= r.status_code < 300, r.json()
        wait_for_hook()

    def commit(self, ref):
        # apparently heads/<branch> ~ refs/heads/<branch> but are not
        # necessarily up to date ??? unlike the git ref system where :ref
        # starts at heads/
        if ref.startswith('heads/'):
            ref = 'refs/' + ref

        r = self._session.get('https://api.github.com/repos/{}/commits/{}'.format(self.name, ref))
        response = r.json()
        assert 200 <= r.status_code < 300, response

        c = response['commit']
        return Commit(
            id=response['sha'],
            tree=c['tree']['sha'],
            message=c['message'],
            author=c['author'],
            committer=c['committer'],
            parents=[p['sha'] for p in response['parents']],
        )

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
            r = self._session.get(t['url'])
            assert 200 <= r.status_code < 300, r.json()
            # assume all test content is textual
            tree[t['path']] = base64.b64decode(r.json()['content']).decode()

        return tree

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

ct = itertools.count()

Commit = collections.namedtuple('Commit', 'id tree message author committer parents')

from odoo.tools.func import lazy_property
class LabelsProxy(collections.abc.MutableSet):
    def __init__(self, pr):
        self._pr = pr

    @property
    def _labels(self):
        pr = self._pr
        r = pr._session.get('https://api.github.com/repos/{}/issues/{}/labels'.format(pr.repo.name, pr.number))
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
        r = pr._session.post('https://api.github.com/repos/{}/issues/{}/labels'.format(pr.repo.name, pr.number), json={
            'labels': [label]
        })
        assert r.ok, r.json()

    def discard(self, label):
        pr = self._pr
        r = pr._session.delete('https://api.github.com/repos/{}/issues/{}/labels/{}'.format(pr.repo.name, pr.number, label))
        # discard should do nothing if the item didn't exist in the set
        assert r.ok or r.status_code == 404, r.json()

    def update(self, *others):
        pr = self._pr
        # because of course that one is not provided by MutableMapping...
        r = pr._session.post('https://api.github.com/repos/{}/issues/{}/labels'.format(pr.repo.name, pr.number), json={
            'labels': list(set(itertools.chain.from_iterable(others)))
        })
        assert r.ok, r.json()

class PR:
    __slots__ = ['number', '_branch', 'repo', 'labels']
    def __init__(self, repo, branch, number):
        """
        :type repo: Repo
        :type branch: str
        :type number: int
        """
        self.number = number
        self._branch = branch
        self.repo = repo
        self.labels = LabelsProxy(self)

    @property
    def _session(self):
        return self.repo._session

    @property
    def _pr(self):
        r = self._session.get('https://api.github.com/repos/{}/pulls/{}'.format(self.repo.name, self.number))
        assert 200 <= r.status_code < 300, r.json()
        return r.json()

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
        r = self._session.get('https://api.github.com/repos/{}/issues/{}/comments'.format(self.repo.name, self.number))
        assert 200 <= r.status_code < 300, r.json()
        return [
            (c['user']['login'], c['body'])
            for c in r.json()
        ]

    def _set_prop(self, prop, value):
        r = self._session.patch('https://api.github.com/repos/{}/pulls/{}'.format(self.repo.name, self.number), json={
            prop: value
        })
        assert 200 <= r.status_code < 300, r.json()
        wait_for_hook()

    @property
    def title(self):
        raise NotImplementedError()
    title = title.setter(lambda self, v: self._set_prop('title', v))

    @property
    def base(self):
        raise NotImplementedError()
    base = base.setter(lambda self, v: self._set_prop('base', v))

    def post_comment(self, body, user):
        r = self._session.post(
            'https://api.github.com/repos/{}/issues/{}/comments'.format(self.repo.name, self.number),
            json={'body': body},
            headers={'Authorization': 'token {}'.format(self.repo._tokens[user])}
        )
        assert 200 <= r.status_code < 300, r.json()
        wait_for_hook()
        return r.json()['id']

    def edit_comment(self, cid, body, user):
        r = self._session.patch(
            'https://api.github.com/repos/{}/issues/comments/{}'.format(self.repo.name, cid),
            json={'body': body},
            headers={'Authorization': 'token {}'.format(self.repo._tokens[user])}
        )
        assert 200 <= r.status_code < 300, r.json()
        wait_for_hook()

    def delete_comment(self, cid, user):
        r = self._session.delete(
            'https://api.github.com/repos/{}/issues/comments/{}'.format(self.repo.name, cid),
            headers={'Authorization': 'token {}'.format(self.repo._tokens[user])}
        )
        assert r.status_code == 204, r.json()
        wait_for_hook()

    def open(self):
        self._set_prop('state', 'open')

    def close(self):
        self._set_prop('state', 'closed')

    def push(self, sha):
        self.repo.update_ref(self._branch, sha, force=True)

    def post_review(self, state, user, body):
        r = self._session.post(
            'https://api.github.com/repos/{}/pulls/{}/reviews'.format(self.repo.name, self.number),
            json={'body': body, 'event': state,},
            headers={'Authorization': 'token {}'.format(self.repo._tokens[user])}
        )
        assert 200 <= r.status_code < 300, r.json()
        wait_for_hook()
