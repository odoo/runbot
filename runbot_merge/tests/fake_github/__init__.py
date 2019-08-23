import collections
import datetime
import hashlib
import hmac
import io
import itertools
import json
import logging
import re

import responses
import werkzeug.urls
import werkzeug.test
import werkzeug.wrappers
from werkzeug.urls import url_parse, url_encode

from . import git

REPOS_API_PATTERN = re.compile(
    r'https://api.github.com/repos/(?P<repo>\w+/\w+)/(?P<path>.+)'
)
USERS_API_PATTERN = re.compile(
    r"https://api.github.com/users/(?P<user>\w+)"
)

class APIResponse(responses.BaseResponse):
    def __init__(self, sim, url):
        super(APIResponse, self).__init__(
            method=None,
            url=url
        )
        self.sim = sim
        self.content_type = 'application/json'
        self.stream = False

    def matches(self, request):
        return self._url_matches(self.url, request.url, self.match_querystring)

    def get_response(self, request):
        m = self.url.match(request.url)

        r = self.dispatch(request, m)
        if isinstance(r, responses.HTTPResponse):
            return r

        (status, r) = r
        headers = self.get_headers()
        if r is None:
            body = io.BytesIO(b'')
            headers['Content-Type'] = 'text/plain'
        else:
            body = io.BytesIO(json.dumps(r).encode('utf-8'))

        return responses.HTTPResponse(
            status=status,
            reason=r.get('message') if isinstance(r, dict) else "bollocks",
            body=body,
            headers=headers,
            preload_content=False, )

class ReposAPIResponse(APIResponse):
    def __init__(self, sim):
        super().__init__(sim, REPOS_API_PATTERN)

    def dispatch(self, request, match):
        return self.sim.repos[match.group('repo')].api(match.group('path'), request)

class UsersAPIResponse(APIResponse):
    def __init__(self, sim):
        super().__init__(sim, url=USERS_API_PATTERN)

    def dispatch(self, request, match):
        return self.sim._read_user(request, match.group('user'))


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
        self._requests.add(ReposAPIResponse(self))
        self._requests.add(UsersAPIResponse(self))
        return self

    def __exit__(self, *args):
        return self._requests.__exit__(*args)

    def _read_user(self, _, user):
        return (200, {
            'id': id(user),
            'type': 'User',
            'login': user,
            'name': user.capitalize(),
        })


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
        self.protected = set()

    def hook(self, hook, events):
        for event in events:
            self.hooks[event].append(Client(*hook))

    def notify(self, event_type, *payload):
        for client in self.hooks.get(event_type, []):
            getattr(client, event_type)(*payload)

    def set_secret(self, secret):
        for clients in self.hooks.values():
            for client in clients:
                client.secret = secret

    def issue(self, number):
        return self.issues[number]

    def make_issue(self, title, body):
        return Issue(self, title, body)

    def make_pr(self, title, body, target, ctid, user, label=None):
        assert 'heads/%s' % target in self.refs
        return PR(self, title, body, target, ctid, user=user, label='{}:{}'.format(user, label or target))

    def get_ref(self, ref):
        if re.match(r'[0-9a-f]{40}', ref):
            return ref

        sha = self.refs.get(ref)
        assert sha, "no ref %s" % ref
        return sha

    def make_ref(self, name, commit, force=False):
        assert isinstance(self.objects[commit], Commit)
        if not force and name in self.refs:
            raise ValueError("ref %s already exists" % name)
        self.refs[name] = commit

    def protect(self, branch):
        ref = 'heads/%s' % branch
        assert ref in self.refs
        self.protected.add(ref)

    def update_ref(self, name, commit, force=False):
        current = self.refs.get(name)
        assert current is not None

        assert name not in self.protected and force or git.is_ancestor(
            self.objects, current, commit)

        self.make_ref(name, commit, force=True)

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
            yield c.to_json()

    def post_status(self, ref, state, context='default', **kw):
        assert state in ('error', 'failure', 'pending', 'success')
        c = self.commit(ref)
        c.statuses.append({'state': state, 'context': context, **kw})
        self.notify('status', self.name, context, state, c.id, kw)

    def make_commit(self, ref, message, author, committer=None, tree=None, wait=True):
        assert tree, "a commit must provide either a full tree"

        refs = ref or []
        if not isinstance(refs, list):
            refs = [ref]

        pids = [
            ref if re.match(r'[0-9a-f]{40}', ref) else self.refs[ref]
            for ref in refs
        ]

        if type(tree) is type(u''):
            assert isinstance(self.objects.get(tree), dict)
            tid = tree
        else:
            tid = self._save_tree(tree)

        c = Commit(tid, message, author, committer or author, parents=pids)
        self.objects[c.id] = c
        if refs and refs[0] != pids[0]:
            self.refs[refs[0]] = c.id
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
        # a better version would be some sort of longest-match?
        for method, pattern, handler in sorted(self._handlers, key=lambda t: -len(t[1])):
            if method and request.method != method:
                continue
            # FIXME: remove qs from path & ensure path is entirely matched, maybe finally use proper routing?
            m = re.match(pattern, path)
            if m:
                return handler(self, request, **m.groupdict())
        return (404, {'message': "No match for {} {}".format(request.method, path)})

    def read_tree(self, commit):
        return git.read_object(self.objects, commit.tree)

    def is_ancestor(self, sha, of):
        assert not git.is_ancestor(self.objects, sha, of=of)

    def _read_ref(self, _, ref):
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

        try:
            self.update_ref(ref, sha, body.get('force') or False)
        except AssertionError:
            return (400, None)

        return (200, {
            "ref": "refs/%s" % ref,
            "object": {
                "type": "commit",
                "sha": sha,
            }
        })

    def _create_commit(self, r):
        body = json.loads(r.body)
        author = body.get('author')
        try:
            sha = self.make_commit(
                ref=body.get('parents'),
                message=body['message'],
                author=author,
                committer=body.get('committer'),
                tree=body['tree']
            )
        except (KeyError, AssertionError):
            # either couldn't find the parent or couldn't find the tree
            return (404, None)

        return (201, self._read_commit(r, sha)[1])

    def _read_commit(self, _, sha):
        c = self.objects.get(sha)
        if not isinstance(c, Commit):
            return (404, None)
        return (200, {
            "sha": sha,
            "author": c.author.to_json(),
            "committer": c.committer.to_json(),
            "message": c.message,
            "tree": {"sha": c.tree},
            "parents": [{"sha": p} for p in c.parents],
        })

    def _read_statuses(self, _, ref):
        try:
            c = self.commit(ref)
        except KeyError:
            return (404, None)

        return (200, {
            'sha': c.id,
            'total_count': len(c.statuses),
            # TODO: combined?
            'statuses': [
                {'description': None, 'target_url': None, **st}
                for st in reversed(c.statuses)]
        })

    def _read_issue(self, r, number):
        try:
            issue = self.issues[int(number)]
        except KeyError:
            return (404, None)
        attr = {'pull_request': True} if isinstance(issue, PR) else {}
        return (200, {'number': issue.number, **attr})

    def _read_issue_comments(self, r, number):
        try:
            issue = self.issues[int(number)]
        except KeyError:
            return (404, None)
        return (200, [{
            'user': {'login': author},
            'body': body,
        } for author, body in issue.comments
          if not body.startswith('REVIEW')
        ])

    def _create_issue_comment(self, r, number):
        try:
            issue = self.issues[int(number)]
        except KeyError:
            return (404, None)
        try:
            body = json.loads(r.body)['body']
        except KeyError:
            return (400, None)

        issue.post_comment(body, "user")
        return (201, {
            'id': 0,
            'body': body,
            'user': { 'login': "user" },
        })

    def _read_pr(self, r, number):
        try:
            pr = self.issues[int(number)]
        except KeyError:
            return (404, None)
        # FIXME: dedup with Client
        return (200, {
            'number': pr.number,
            'head': {
                'sha': pr.head,
                'label': pr.label,
            },
            'base': {
                'ref': pr.base,
                'repo': {
                    'name': self.name.split('/')[1],
                    'full_name': self.name,
                },
            },
            'title': pr.title,
            'body': pr.body,
            'commits': len(pr.commits),
            'user': {'login': pr.user},
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
            self.notify('pull_request', 'reopened', pr)
        elif body.get('state') == 'closed':
            self.notify('pull_request', 'closed', pr)

        return (200, {})

    def _read_pr_reviews(self, _, number):
        pr = self.issues.get(int(number))
        if not isinstance(pr, PR):
            return (404, None)

        return (200, [{
            'user': {'login': author},
            'state': r.group(1),
            'body': r.group(2),
        }
            for author, body in pr.comments
            for r in [re.match(r'REVIEW (\w+)\n\n(.*)', body)]
            if r
        ])

    def _read_pr_commits(self, r, number):
        pr = self.issues.get(int(number))
        if not isinstance(pr, PR):
            return (404, None)

        url = url_parse(r.url)
        qs = url.decode_query()
        # github pages are 1-indexeds
        page = int(qs.get('page') or 1) - 1
        per_page = int(qs.get('per_page') or 100)

        offset = page * per_page
        limit = page + 1 * per_page
        headers = {'Content-Type': 'application/json'}
        if len(pr.commits) > limit:
            nextlink = url.replace(query=url_encode(dict(qs, page=page+1)))
            headers['Link'] = '<%s>; rel="next"' % str(nextlink)

        commits = [
            c.to_json()
            for c in sorted(
                pr.commits,
                key=lambda c: (c.author.date, c.committer.date)
            )[offset:limit]
        ]
        body = io.BytesIO(json.dumps(commits).encode('utf-8'))

        return responses.HTTPResponse(
            status=200, reason="OK",
            headers=headers,
            body=body, preload_content=False,
        )

    def _get_labels(self, r, number):
        try:
            pr = self.issues[int(number)]
        except KeyError:
            return (404, None)

        return (200, [{'name': label} for label in pr.labels])

    def _reset_labels(self, r, number):
        try:
            pr = self.issues[int(number)]
        except KeyError:
            return (404, None)

        pr.labels = set(json.loads(r.body)['labels'])

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
            merge_base = git.merge_base(self.objects, target, sha)
        except Exception:
            return (400, {'message': "No common ancestor between %(base)s and %(head)s" % body})
        try:
            tid = git.merge_objects(
                self.objects,
                self.objects[merge_base].tree,
                self.objects[target].tree,
                self.objects[sha].tree,
            )
        except Exception as e:
            logging.exception("Merge Conflict")
            return (409, {'message': 'Merge Conflict %r' % e})

        c = Commit(tid, body['commit_message'], author=None, committer=None, parents=[target, sha])
        self.objects[c.id] = c
        self.refs[base] = c.id

        return (201, c.to_json())

    _handlers = [
        ('POST', r'git/refs', _create_ref),
        ('GET', r'git/refs/(?P<ref>.*)', _read_ref),
        ('PATCH', r'git/refs/(?P<ref>.*)', _write_ref),

        # nb: there's a different commits at /commits with repo-level metadata
        ('GET', r'git/commits/(?P<sha>[0-9A-Fa-f]{40})', _read_commit),
        ('POST', r'git/commits', _create_commit),
        ('GET', r'commits/(?P<ref>[^/]+)/status', _read_statuses),

        ('GET', r'issues/(?P<number>\d+)', _read_issue),
        ('GET', r'issues/(?P<number>\d+)/comments', _read_issue_comments),
        ('POST', r'issues/(?P<number>\d+)/comments', _create_issue_comment),

        ('POST', r'merges', _do_merge),

        ('GET', r'pulls/(?P<number>\d+)', _read_pr),
        ('PATCH', r'pulls/(?P<number>\d+)', _edit_pr),
        ('GET', r'pulls/(?P<number>\d+)/reviews', _read_pr_reviews),
        ('GET', r'pulls/(?P<number>\d+)/commits', _read_pr_commits),

        ('GET', r'issues/(?P<number>\d+)/labels', _get_labels),
        ('PUT', r'issues/(?P<number>\d+)/labels', _reset_labels),
    ]

class Issue(object):
    def __init__(self, repo, title, body):
        self.repo = repo
        self._title = title
        self._body = body
        self.number = max(repo.issues or [0]) + 1
        self._comments = []
        self.labels = set()
        repo.issues[self.number] = self

    @property
    def comments(self):
        return [(c.user, c.body) for c in self._comments]

    def post_comment(self, body, user):
        c = Comment(user, body)
        self._comments.append(c)
        self.repo.notify('issue_comment', self, 'created', c)
        return c.id

    def edit_comment(self, cid, newbody, user):
        c = next(c for c in self._comments if c.id == cid)
        c.body = newbody
        self.repo.notify('issue_comment', self, 'edited', c)

    def delete_comment(self, cid, user):
        c = next(c for c in self._comments if c.id == cid)
        self._comments.remove(c)
        self.repo.notify('issue_comment', self, 'deleted', c)

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
class Comment:
    _cseq = itertools.count()
    def __init__(self, user, body, id=None):
        self.user = user
        self.body = body
        self.id = id or next(self._cseq)

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

        repo.notify('pull_request', 'opened', self)

    @Issue.title.setter
    def title(self, value):
        old = self.title
        Issue.title.fset(self, value)
        self.repo.notify('pull_request', 'edited', self, {
            'title': {'from': old}
        })
    @Issue.body.setter
    def body(self, value):
        old = self.body
        Issue.body.fset(self, value)
        self.repo.notify('pull_request', 'edited', self, {
            'body': {'from': old}
        })
    @property
    def base(self):
        return self._base
    @base.setter
    def base(self, value):
        old, self._base = self._base, value
        self.repo.notify('pull_request', 'edited', self, {
            'base': {'ref': {'from': old}}
        })

    def push(self, sha):
        self.head = sha
        self.repo.notify('pull_request', 'synchronize', self)

    def open(self):
        assert self.state == 'closed'
        self.state = 'open'
        self.repo.notify('pull_request', 'reopened', self)

    def close(self):
        self.state = 'closed'
        self.repo.notify('pull_request', 'closed', self)

    @property
    def commits(self):
        store = self.repo.objects
        target = self.repo.commit('heads/%s' % self.base).id

        base = {h for h, _ in git.walk_ancestors(store, target, False)}
        own = [
            h for h, _ in git.walk_ancestors(store, self.head, False)
            if h not in base
        ]
        return list(map(self.repo.commit, reversed(own)))

    def post_review(self, state, user, body):
        self.comments.append((user, "REVIEW %s\n\n%s " % (state, body)))
        self.repo.notify('pull_request_review', state, self, user, body)

FMT = '%Y-%m-%dT%H:%M:%SZ'
class Author(object):
    __slots__ = ['name', 'email', 'date']

    def __init__(self, name, email, date):
        self.name = name
        self.email = email
        self.date = date or datetime.datetime.now().strftime(FMT)

    @classmethod
    def from_(cls, d):
        if not d:
            return None
        return Author(**d)

    def to_json(self):
        return {
            'name': self.name,
            'email': self.email,
            'date': self.date,
        }

    def __str__(self):
        return '%s <%s> %d Z' % (
            self.name,
            self.email,
            int(datetime.datetime.strptime(self.date, FMT).timestamp())
        )

class Commit(object):
    __slots__ = ['tree', 'message', 'author', 'committer', 'parents', 'statuses']
    def __init__(self, tree, message, author, committer, parents):
        self.tree = tree
        self.message = message.strip()
        self.author = Author.from_(author) or Author('', '', '')
        self.committer = Author.from_(committer) or self.author
        self.parents = parents
        self.statuses = []

    @property
    def id(self):
        return git.make_commit(self.tree, self.message, self.author, self.committer, parents=self.parents)[0]

    def to_json(self):
        return {
            "sha": self.id,
            "commit": {
                "author": self.author.to_json(),
                "committer": self.committer.to_json(),
                "message": self.message,
                "tree": {"sha": self.tree},
            },
            "parents": [{"sha": p} for p in self.parents]
        }

    def __str__(self):
        parents = '\n'.join('parent {}'.format(p) for p in self.parents) + '\n'
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
        self.secret = None
        super(Client, self).__init__(application, werkzeug.wrappers.BaseResponse)

    def _make_env(self, event_type, data):
        headers = [('X-Github-Event', event_type)]
        body = json.dumps(data).encode('utf-8')
        if self.secret:
            sig = hmac.new(self.secret.encode('ascii'), body, hashlib.sha1).hexdigest()
            headers.append(('X-Hub-Signature', 'sha1=' + sig))

        return werkzeug.test.EnvironBuilder(
            path=self._webhook_path,
            method='POST',
            headers=headers,
            content_type='application/json',
            data=body,
        )
    def _repo(self, name):
        return {
            'name': name.split('/')[1],
            'full_name': name,
        }

    def pull_request(self, action, pr, changes=None):
        assert action in ('opened', 'reopened', 'closed', 'synchronize', 'edited')
        return self.open(self._make_env(
            'pull_request', {
                'action': action,
                'pull_request': self._pr(pr),
                'repository': self._repo(pr.repo.name),
                'sender': {'login': '<>'},
                **({'changes': changes} if changes else {})
            }
        ))

    def pull_request_review(self, action, pr, user, body):
        """
        :type action: 'APPROVE' | 'REQUEST_CHANGES' | 'COMMENT'
        :type pr: PR
        :type user: str
        :type body: str
        """
        assert action in ('APPROVE', 'REQUEST_CHANGES', 'COMMENT')
        return self.open(self._make_env(
            'pull_request_review', {
                'action': 'submitted',
                'review': {
                    'state': 'APPROVED' if action == 'APPROVE' else action,
                    'body': body,
                    'user': {'login': user},
                },
                'pull_request': self._pr(pr),
                'repository': self._repo(pr.repo.name),
            }
        ))

    def status(self, repository, context, state, sha, kw):
        assert state in ('success', 'failure', 'pending')
        return self.open(self._make_env(
            'status', {
                'name': repository,
                'context': context,
                'state': state,
                'sha': sha,
                'repository': self._repo(repository),
                'target_url': None,
                'description': None,
                **(kw or {})
            }
        ))

    def issue_comment(self, issue, action, comment):
        assert action in ('created', 'edited', 'deleted')
        contents = {
            'action': action,
            'issue': { 'number': issue.number },
            'repository': self._repo(issue.repo.name),
            'comment': { 'id': comment.id, 'body': comment.body, 'user': {'login': comment.user } },
        }
        if isinstance(issue, PR):
            contents['issue']['pull_request'] = { 'url': 'fake' }
        return self.open(self._make_env('issue_comment', contents))

    def _pr(self, pr):
        """
        :type pr: PR
        """
        return {
            'number': pr.number,
            'head': {
                'sha': pr.head,
                'label': pr.label,
            },
            'base': {
                'ref': pr.base,
                'repo': self._repo(pr.repo.name),
            },
            'title': pr.title,
            'body': pr.body,
            'commits': len(pr.commits),
            'user': {'login': pr.user},
        }
