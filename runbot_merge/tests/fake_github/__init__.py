import collections
import io
import itertools
import json
import logging
import re

import responses
import werkzeug.test
import werkzeug.wrappers

from . import git

API_PATTERN = re.compile(
    r'https://api.github.com/repos/(?P<repo>\w+/\w+)/(?P<path>.+)'
)
class APIResponse(responses.BaseResponse):
    def __init__(self, sim):
        super(APIResponse, self).__init__(
            method=None,
            url=API_PATTERN
        )
        self.sim = sim
        self.content_type = 'application/json'
        self.stream = False

    def matches(self, request):
        return self._url_matches(self.url, request.url, self.match_querystring)

    def get_response(self, request):
        m = self.url.match(request.url)

        (status, r) = self.sim.repos[m.group('repo')].api(m.group('path'), request)

        headers = self.get_headers()
        body = io.BytesIO(b'')
        if r:
            body = io.BytesIO(json.dumps(r).encode('utf-8'))

        return responses.HTTPResponse(
            status=status,
            reason=r.get('message') if r else "bollocks",
            body=body,
            headers=headers,
            preload_content=False, )

class Github(object):
    """ Github simulator

    When enabled (by context-managing):

    * intercepts all ``requests`` calls & replies to api.github.com
    * sends relevant hooks (registered per-repo as pairs of WSGI app and URL)
    * stores repo content
    """
    def __init__(self):
        # {repo: {name, issues, objects, refs, hooks}}
        self.repos = {}

    def repo(self, name, hooks=()):
        r = self.repos[name] = Repo(name)
        for hook, events in hooks:
            r.hook(hook, events)
        return self.repos[name]

    def __enter__(self):
        # otherwise swallows errors from within the test
        self._requests = responses.RequestsMock(assert_all_requests_are_fired=False).__enter__()
        self._requests.add(APIResponse(self))
        return self

    def __exit__(self, *args):
        return self._requests.__exit__(*args)

class Repo(object):
    def __init__(self, name):
        self.name = name
        self.issues = {}
        #: we're cheating, instead of storing serialised in-memory
        #: objects we're storing the Python stuff directly, Commit
        #: objects for commits, {str: hash} for trees and bytes for
        #: blobs. We're still indirecting via hashes and storing a
        #: h:o map because going through the API probably requires it
        self.objects = {}
        # branches: refs/heads/*
        # PRs: refs/pull/*
        self.refs = {}
        # {event: (wsgi_app, url)}
        self.hooks = collections.defaultdict(list)

    def hook(self, hook, events):
        for event in events:
            self.hooks[event].append(Client(*hook))

    def notify(self, event_type, *payload):
        for client in self.hooks.get(event_type, []):
            getattr(client, event_type)(*payload)

    def issue(self, number):
        return self.issues[number]

    def make_issue(self, title, body):
        return Issue(self, title, body)

    def make_pr(self, title, body, target, ctid, user, label=None):
        assert 'heads/%s' % target in self.refs
        return PR(self, title, body, target, ctid, user=user, label=label or '{}:{}'.format(user, target))

    def make_ref(self, name, commit, force=False):
        assert isinstance(self.objects[commit], Commit)
        if not force and name in self.refs:
            raise ValueError("ref %s already exists" % name)
        self.refs[name] = commit

    def commit(self, ref):
        sha = self.refs.get(ref) or ref
        commit = self.objects[sha]
        assert isinstance(commit, Commit)
        return commit

    def log(self, ref):
        commits = [self.commit(ref)]
        while commits:
            c = commits.pop(0)
            commits.extend(self.commit(r) for r in c.parents)
            yield c

    def post_status(self, ref, status, context='default', description=""):
        assert status in ('error', 'failure', 'pending', 'success')
        c = self.commit(ref)
        c.statuses.append((status, context, description))
        self.notify('status', self.name, context, status, c.id)

    def make_commit(self, ref, message, author, committer=None, tree=None, changes=None):
        assert (tree is None) ^ (changes is None), \
            "a commit must provide either a full tree or changes to the previous tree"

        branch = False
        if ref is None:
            pids = []
        else:
            pid = ref
            if not re.match(r'[0-9a-f]{40}', ref):
                pid = self.refs[ref]
                branch = True
            parent = self.objects[pid]
            pids = [pid]

        if tree is None:
            # TODO?
            tid = self._update_tree(parent.tree, changes)
        elif type(tree) is type(u''):
            assert isinstance(self.objects.get(tree), dict)
            tid = tree
        else:
            tid = self._save_tree(tree)

        c = Commit(tid, message, author, committer or author, parents=pids)
        self.objects[c.id] = c
        if branch:
            self.refs[ref] = c.id
        return c.id

    def _save_tree(self, t):
        """ t: Dict String (String | Tree)
        """
        t = {name: self._make_obj(obj) for name, obj in t.items()}
        h, _ = git.make_tree(
            self.objects,
            t
        )
        self.objects[h] = t
        return h

    def _make_obj(self, o):
        if type(o) is type(u''):
            o = o.encode('utf-8')

        if type(o) is bytes:
            h, b = git.make_blob(o)
            self.objects[h] = o
            return h
        return self._save_tree(o)

    def api(self, path, request):
        for method, pattern, handler in self._handlers:
            if method and request.method != method:
                continue

            m = re.match(pattern, path)
            if m:
                return handler(self, request, **m.groupdict())
        return (404, {'message': "No match for {} {}".format(request.method, path)})

    def _read_ref(self, r, ref):
        obj = self.refs.get(ref)
        if obj is None:
            return (404, None)
        return (200, {
            "ref": "refs/%s" % ref,
            "object": {
                "type": "commit",
                "sha": obj,
            }
        })
    def _create_ref(self, r):
        body = json.loads(r.body)
        ref = body['ref']
        # ref must start with refs/ and contain at least two slashes
        if not (ref.startswith('refs/') and ref.count('/') >= 2):
            return (400, None)
        ref = ref[5:]
        # if ref already exists conflict?
        if ref in self.refs:
            return (409, None)

        sha = body['sha']
        obj = self.objects.get(sha)
        # if sha is not in the repo or not a commit, 404
        if not isinstance(obj, Commit):
            return (404, None)

        self.make_ref(ref, sha)

        return (201, {
            "ref": "refs/%s" % ref,
            "object": {
                "type": "commit",
                "sha": sha,
            }
        })

    def _write_ref(self, r, ref):
        current = self.refs.get(ref)
        if current is None:
            return (404, None)
        body = json.loads(r.body)
        sha = body['sha']
        if sha not in self.objects:
            return (404, None)

        if not body.get('force'):
            if not git.is_ancestor(self.objects, current, sha):
                return (400, None)

        self.make_ref(ref, sha, force=True)
        return (200, {
            "ref": "refs/%s" % ref,
            "object": {
                "type": "commit",
                "sha": sha,
            }
        })

    def _create_commit(self, r):
        body = json.loads(r.body)
        [parent] = body.get('parents') or [None]
        author = body.get('author') or {'name': 'default', 'email': 'default', 'date': 'Z'}
        try:
            sha = self.make_commit(
                ref=parent,
                message=body['message'],
                author=author,
                committer=body.get('committer') or author,
                tree=body['tree']
            )
        except (KeyError, AssertionError):
            # either couldn't find the parent or couldn't find the tree
            return (404, None)

        return (201, {
            "sha": sha,
            "author": author,
            "committer": body.get('committer') or author,
            "message": body['message'],
            "tree": {"sha": body['tree']},
            "parents": [{"sha": sha}],
        })
    def _read_commit(self, r, sha):
        c = self.objects.get(sha)
        if not isinstance(c, Commit):
            return (404, None)
        return (200, {
            "sha": sha,
            "author": c.author,
            "committer": c.committer,
            "message": c.message,
            "tree": {"sha": c.tree},
            "parents": [{"sha": p} for p in c.parents],
        })

    def _create_issue_comment(self, r, number):
        try:
            issue = self.issues[int(number)]
        except KeyError:
            return (404, None)
        try:
            body = json.loads(r.body)['body']
        except KeyError:
            return (400, None)

        issue.post_comment(body, "<insert current user here>")
        return (201, {
            'id': 0,
            'body': body,
            'user': { 'login': "<insert current user here>" },
        })

    def _edit_pr(self, r, number):
        try:
            pr = self.issues[int(number)]
        except KeyError:
            return (404, None)

        body = json.loads(r.body)
        if not body.keys() & {'title', 'body', 'state', 'base'}:
            # FIXME: return PR content
            return (200, {})
        assert body.get('state') in ('open', 'closed', None)

        pr.state = body.get('state') or pr.state
        if body.get('title'):
            pr.title = body.get('title')
        if body.get('body'):
            pr.body = body.get('body')
        if body.get('base'):
            pr.base = body.get('base')

        if body.get('state') == 'open':
            self.notify('pull_request', 'reopened', self.name, pr)
        elif body.get('state') == 'closed':
            self.notify('pull_request', 'closed', self.name, pr)

        return (200, {})

    def _do_merge(self, r):
        body = json.loads(r.body) # {base, head, commit_message}
        if not body.get('commit_message'):
            return (400, {'message': "Merges require a commit message"})
        base = 'heads/%s' % body['base']
        target = self.refs.get(base)
        if not target:
            return (404, {'message': "Base does not exist"})
        # head can be either a branch or a sha
        sha = self.refs.get('heads/%s' % body['head']) or body['head']
        if sha not in self.objects:
            return (404, {'message': "Head does not exist"})

        if git.is_ancestor(self.objects, sha, of=target):
            return (204, None)

        # merging according to read-tree:
        # get common ancestor (base) of commits
        try:
            base = git.merge_base(self.objects, target, sha)
        except Exception as e:
            return (400, {'message': "No common ancestor between %(base)s and %(head)s" % body})
        try:
            tid = git.merge_objects(
                self.objects,
                self.objects[base].tree,
                self.objects[target].tree,
                self.objects[sha].tree,
            )
        except Exception as e:
            logging.exception("Merge Conflict")
            return (409, {'message': 'Merge Conflict %r' % e})

        c = Commit(tid, body['commit_message'], author=None, committer=None, parents=[target, sha])
        self.objects[c.id] = c

        return (201, {
            "sha": c.id,
            "commit": {
                "author": c.author,
                "committer": c.committer,
                "message": body['commit_message'],
                "tree": {"sha": tid},
            },
            "parents": [{"sha": target}, {"sha": sha}]
        })

    _handlers = [
        ('POST', r'git/refs', _create_ref),
        ('GET', r'git/refs/(?P<ref>.*)', _read_ref),
        ('PATCH', r'git/refs/(?P<ref>.*)', _write_ref),

        # nb: there's a different commits at /commits with repo-level metadata
        ('GET', r'git/commits/(?P<sha>[0-9A-Fa-f]{40})', _read_commit),
        ('POST', r'git/commits', _create_commit),

        ('POST', r'issues/(?P<number>\d+)/comments', _create_issue_comment),

        ('POST', r'merges', _do_merge),

        ('PATCH', r'pulls/(?P<number>\d+)', _edit_pr),
    ]

class Issue(object):
    def __init__(self, repo, title, body):
        self.repo = repo
        self._title = title
        self._body = body
        self.number = max(repo.issues or [0]) + 1
        self.comments = []
        repo.issues[self.number] = self

    def post_comment(self, body, user):
        self.comments.append((user, body))
        self.repo.notify('issue_comment', self, user, body)

    @property
    def title(self):
        return self._title
    @title.setter
    def title(self, value):
        self._title = value

    @property
    def body(self):
        return self._body
    @body.setter
    def body(self, value):
        self._body = value

class PR(Issue):
    def __init__(self, repo, title, body, target, ctid, user, label):
        super(PR, self).__init__(repo, title, body)
        assert ctid in repo.objects
        repo.refs['pull/%d' % self.number] = ctid
        self.head = ctid
        self._base = target
        self.user = user
        self.label = label
        self.state = 'open'

        repo.notify('pull_request', 'opened', repo.name, self)

    @Issue.title.setter
    def title(self, value):
        old = self.title
        Issue.title.fset(self, value)
        self.repo.notify('pull_request', 'edited', self.repo.name, self, {
            'title': {'from': old}
        })
    @Issue.body.setter
    def body(self, value):
        old = self.body
        Issue.body.fset(self, value)
        self.repo.notify('pull_request', 'edited', self.repo.name, self, {
            'body': {'from': old}
        })
    @property
    def base(self):
        return self._base
    @base.setter
    def base(self, value):
        old, self._base = self._base, value
        self.repo.notify('pull_request', 'edited', self.repo.name, self, {
            'base': {'from': {'ref': old}}
        })

    def push(self, sha):
        self.head = sha
        self.repo.notify('pull_request', 'synchronize', self.repo.name, self)

    def open(self):
        assert self.state == 'closed'
        self.state = 'open'
        self.repo.notify('pull_request', 'reopened', self.repo.name, self)

    def close(self):
        self.state = 'closed'
        self.repo.notify('pull_request', 'closed', self.repo.name, self)

    @property
    def commits(self):
        store = self.repo.objects
        target = self.repo.commit('heads/%s' % self.base).id
        return len({h for h, _ in git.walk_ancestors(store, self.head, False)}
                   - {h for h, _ in git.walk_ancestors(store, target, False)})

class Commit(object):
    __slots__ = ['tree', 'message', 'author', 'committer', 'parents', 'statuses']
    def __init__(self, tree, message, author, committer, parents):
        self.tree = tree
        self.message = message
        self.author = author
        self.committer = committer or author
        self.parents = parents
        self.statuses = []

    @property
    def id(self):
        return git.make_commit(self.tree, self.message, self.author, self.committer, parents=self.parents)[0]

    def __str__(self):
        parents = '\n'.join('parent {p}' for p in self.parents) + '\n'
        return """commit {}
tree {}
{}author {}
committer {}

{}""".format(
    self.id,
    self.tree,
    parents,
    self.author,
    self.committer,
    self.message
)

class Client(werkzeug.test.Client):
    def __init__(self, application, path):
        self._webhook_path = path
        super(Client, self).__init__(application, werkzeug.wrappers.BaseResponse)

    def _make_env(self, event_type, data):
        return werkzeug.test.EnvironBuilder(
            path=self._webhook_path,
            method='POST',
            headers=[('X-Github-Event', event_type)],
            content_type='application/json',
            data=json.dumps(data),
        )

    def pull_request(self, action, repository, pr, changes=None):
        assert action in ('opened', 'reopened', 'closed', 'synchronize', 'edited')
        return self.open(self._make_env(
            'pull_request', {
                'action': action,
                'pull_request': {
                    'number': pr.number,
                    'head': {
                        'sha': pr.head,
                        'label': pr.label,
                    },
                    'base': {
                        'ref': pr.base,
                        'repo': {
                            'name': repository.split('/')[1],
                            'full_name': repository,
                        },
                    },
                    'title': pr.title,
                    'body': pr.body,
                    'commits': pr.commits,
                    'user': { 'login': pr.user },
                },
                **({'changes': changes} if changes else {})
            }
        ))

    def status(self, repository, context, state, sha):
        assert state in ('success', 'failure', 'pending')
        return self.open(self._make_env(
            'status', {
                'name': repository,
                'context': context,
                'state': state,
                'sha': sha,
            }
        ))

    def issue_comment(self, issue, user, body):
        contents = {
            'action': 'created',
            'issue': { 'number': issue.number },
            'repository': { 'name': issue.repo.name.split('/')[1], 'full_name': issue.repo.name },
            'sender': { 'login': user },
            'comment': { 'body': body },
        }
        if isinstance(issue, PR):
            contents['issue']['pull_request'] = { 'url': 'fake' }
        return self.open(self._make_env('issue_comment', contents))
