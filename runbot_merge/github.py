import collections
import functools
import itertools
import logging

import requests

from . import exceptions

_logger = logging.getLogger(__name__)
class GH(object):
    def __init__(self, token, repo):
        self._url = 'https://api.github.com'
        self._repo = repo
        session = self._session = requests.Session()
        session.headers['Authorization'] = 'token {}'.format(token)

    def __call__(self, method, path, json=None, check=True):
        """
        :type check: bool | dict[int:Exception]
        """
        r = self._session.request(
            method,
            '{}/repos/{}/{}'.format(self._url, self._repo, path),
            json=json
        )
        if check:
            if isinstance(check, collections.Mapping):
                exc = check.get(r.status_code)
                if exc:
                    raise exc(r.content)
            r.raise_for_status()
        return r

    def head(self, branch):
        d = self('get', 'git/refs/heads/{}'.format(branch)).json()

        assert d['ref'] == 'refs/heads/{}'.format(branch)
        assert d['object']['type'] == 'commit'
        return d['object']['sha']

    def commit(self, sha):
        return self('GET', 'git/commits/{}'.format(sha)).json()

    def comment(self, pr, message):
        self('POST', 'issues/{}/comments'.format(pr), json={'body': message})

    def close(self, pr, message):
        self.comment(pr, message)
        self('PATCH', 'pulls/{}'.format(pr), json={'state': 'closed'})

    def change_tags(self, pr, from_, to_):
        to_add, to_remove = to_ - from_, from_ - to_
        for t in to_remove:
            r = self('DELETE', 'issues/{}/labels/{}'.format(pr, t), check=False)
            r.raise_for_status()
            # successful deletion or attempt to delete a tag which isn't there
            # is fine, otherwise trigger an error
            if r.status_code not in (200, 404):
                r.raise_for_status()

        if to_add:
            self('POST', 'issues/{}/labels'.format(pr), json=list(to_add))

    def fast_forward(self, branch, sha):
        try:
            self('patch', 'git/refs/heads/{}'.format(branch), json={'sha': sha})
        except requests.HTTPError:
            raise exceptions.FastForwardError()

    def set_ref(self, branch, sha):
        # force-update ref
        r = self('patch', 'git/refs/heads/{}'.format(branch), json={
            'sha': sha,
            'force': True,
        }, check=False)
        if r.status_code == 200:
            return

        # 422 makes no sense but that's what github returns, leaving 404 just
        # in case
        if r.status_code in (404, 422):
            # fallback: create ref
            r = self('post', 'git/refs', json={
                'ref': 'refs/heads/{}'.format(branch),
                'sha': sha,
            }, check=False)
            if r.status_code == 201:
                return
        raise AssertionError("{}: {}".format(r.status_code, r.json()))

    def merge(self, sha, dest, message, squash=False, author=None):
        if not squash:
            r = self('post', 'merges', json={
                'base': dest,
                'head': sha,
                'commit_message': message,
            }, check={409: exceptions.MergeError})
            r = r.json()
            return dict(r['commit'], sha=r['sha'])

        current_head = self.head(dest)
        tree = self.merge(sha, dest, "temp")['tree']['sha']
        c = self('post', 'git/commits', json={
            'message': message,
            'tree': tree,
            'parents': [current_head],
            'author': author,
        }, check={409: exceptions.MergeError}).json()
        self.set_ref(dest, c['sha'])
        return c

    # fetch various bits of issues / prs to load them
    def pr(self, number):
        return (
            self('get', 'issues/{}'.format(number)).json(),
            self('get', 'pulls/{}'.format(number)).json()
        )

    def comments(self, number):
        for page in itertools.count(1):
            r = self('get', 'issues/{}/comments?page={}'.format(number, page))
            yield from r.json()
            if not r.links.get('next'):
                return

    def reviews(self, number):
        for page in itertools.count(1):
            r = self('get', 'pulls/{}/reviews?page={}'.format(number, page))
            yield from r.json()
            if not r.links.get('next'):
                return

    def statuses(self, h):
        r = self('get', 'commits/{}/status'.format(h)).json()
        return [{
            'sha': r['sha'],
            'context': s['context'],
            'state': s['state'],
        } for s in r['statuses']]
