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

    def __call__(self, method, path, params=None, json=None, check=True):
        """
        :type check: bool | dict[int:Exception]
        """
        r = self._session.request(
            method,
            '{}/repos/{}/{}'.format(self._url, self._repo, path),
            params=params,
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

    def merge(self, sha, dest, message):
        r = self('post', 'merges', json={
            'base': dest,
            'head': sha,
            'commit_message': message,
        }, check={409: exceptions.MergeError})
        r = r.json()
        return dict(r['commit'], sha=r['sha'])

    def rebase(self, pr, dest, reset=False, commits=None):
        """ Rebase pr's commits on top of dest, updates dest unless ``reset``
        is set.

        Returns the hash of the rebased head.
        """
        original_head = self.head(dest)
        if commits is None:
            commits = self.commits(pr)

        assert commits, "can't rebase a PR with no commits"
        for c in commits:
            assert len(c['parents']) == 1, "can't rebase commits with more than one parent"
            tmp_msg = 'temp rebasing PR %s (%s)' % (pr, c['sha'])
            c['new_tree'] = self.merge(c['sha'], dest, tmp_msg)['tree']['sha']
        self.set_ref(dest, original_head)

        prev = original_head
        for c in commits:
            copy = self('post', 'git/commits', json={
                'message': c['commit']['message'],
                'tree': c['new_tree'],
                'parents': [prev],
                'author': c['commit']['author'],
                'committer': c['commit']['committer'],
            }, check={409: exceptions.MergeError}).json()
            prev = copy['sha']

        if reset:
            self.set_ref(dest, original_head)

        # prev is updated after each copy so it's the rebased PR head
        return prev

    # fetch various bits of issues / prs to load them
    def pr(self, number):
        return (
            self('get', 'issues/{}'.format(number)).json(),
            self('get', 'pulls/{}'.format(number)).json()
        )

    def comments(self, number):
        for page in itertools.count(1):
            r = self('get', 'issues/{}/comments'.format(number), params={'page': page})
            yield from r.json()
            if not r.links.get('next'):
                return

    def reviews(self, number):
        for page in itertools.count(1):
            r = self('get', 'pulls/{}/reviews'.format(number), params={'page': page})
            yield from r.json()
            if not r.links.get('next'):
                return

    def commits(self, pr):
        """ Returns a PR's commits oldest first (that's what GH does &
        is what we want)
        """
        r = self('get', 'pulls/{}/commits'.format(pr), params={'per_page': PR_COMMITS_MAX})
        assert not r.links.get('next'), "more than {} commits".format(PR_COMMITS_MAX)
        return r.json()

    def statuses(self, h):
        r = self('get', 'commits/{}/status'.format(h)).json()
        return [{
            'sha': r['sha'],
            'context': s['context'],
            'state': s['state'],
        } for s in r['statuses']]

PR_COMMITS_MAX = 50
