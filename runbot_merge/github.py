import collections
import functools
import logging
import pprint

import requests

from odoo.exceptions import UserError
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

        if r.status_code == 404:
            # fallback: create ref
            r = self('post', 'git/refs', json={
                'ref': 'refs/heads/{}'.format(branch),
                'sha': sha,
            }, check=False)
            if r.status_code == 201:
                return
        r.raise_for_status()

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

    # --

    def prs(self):
        cursor = None
        owner, name = self._repo.split('/')
        while True:
            response = self._session.post('{}/graphql'.format(self._url), json={
                'query': PR_QUERY,
                'variables': {
                    'owner': owner,
                    'name': name,
                    'cursor': cursor,
                }
            }).json()

            result = response['data']['repository']['pullRequests']
            for pr in result['nodes']:
                statuses = into(pr, 'headRef.target.status.contexts') or []

                author = into(pr, 'author.login') or into(pr, 'headRepositoryOwner.login')
                source = into(pr, 'headRepositoryOwner.login') or into(pr, 'author.login')
                label = source and "{}:{}".format(source, pr['headRefName'])
                yield {
                    'number': pr['number'],
                    'title': pr['title'],
                    'body': pr['body'],
                    'head': {
                        'ref': pr['headRefName'],
                        'sha': pr['headRefOid'],
                        # headRef may be null if the pr branch was ?deleted?
                        # (mostly closed PR concerns?)
                        'statuses': {
                            c['context']: c['state']
                            for c in statuses
                        },
                        'label': label,
                    },
                    'state': pr['state'].lower(),
                    'user': {'login': author},
                    'base': {
                        'ref': pr['baseRefName'],
                        'repo': {
                            'full_name': pr['repository']['nameWithOwner'],
                        }
                    },
                    'commits': pr['commits']['totalCount'],
                }

            if result['pageInfo']['hasPreviousPage']:
                cursor = result['pageInfo']['startCursor']
            else:
                break
def into(d, path):
    return functools.reduce(
        lambda v, segment: v and v.get(segment),
        path.split('.'),
        d
    )

PR_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  rateLimit { remaining }
  repository(owner: $owner, name: $name) {
    pullRequests(last: 100, before: $cursor) {
      pageInfo { startCursor hasPreviousPage }
      nodes {
        author { # optional
          login
        }
        number
        title
        body
        state
        repository { nameWithOwner }
        baseRefName
        headRefOid
        headRepositoryOwner { # optional
          login
        }
        headRefName
        headRef { # optional
          target {
            ... on Commit {
              status {
                contexts {
                  context
                  state
                }
              }
            }
          }
        }
        commits { totalCount }
        #comments(last: 100) {
        #  nodes {
        #    author {
        #      login
        #    }
        #    body
        #    bodyText
        #  }
        #}
      }
    }
  }
}
"""
