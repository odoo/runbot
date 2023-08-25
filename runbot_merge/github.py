import collections.abc
import itertools
import json
import logging
import logging.handlers
import os
import pathlib
import pprint
import time
import unicodedata
from typing import Iterable, List, TypedDict, Literal

import requests
import werkzeug.urls

import odoo.netsvc
from odoo.tools import topological_sort, config
from . import exceptions, utils

class MergeError(Exception): ...

def _is_json(r):
    return r and r.headers.get('content-type', '').startswith(('application/json', 'application/javascript'))

_logger = logging.getLogger(__name__)
_gh = logging.getLogger('github_requests')
def _init_gh_logger():
    """ Log all GH requests / responses so we have full tracking, but put them
    in a separate file if we're logging to a file
    """
    if not config['logfile']:
        return
    original = pathlib.Path(config['logfile'])
    new = original.with_name('github_requests')\
                  .with_suffix(original.suffix)

    if os.name == 'posix':
        handler = logging.handlers.WatchedFileHandler(str(new))
    else:
        handler = logging.FileHandler(str(new))

    handler.setFormatter(odoo.netsvc.DBFormatter(
        '%(asctime)s %(pid)s %(levelname)s %(dbname)s %(name)s: %(message)s'
    ))
    _gh.addHandler(handler)
    _gh.propagate = False

if odoo.netsvc._logger_init:
    _init_gh_logger()

SimpleUser = TypedDict('SimpleUser', {
    'login': str,
    'url': str,
    'type': Literal['User', 'Organization'],
})
Authorship = TypedDict('Authorship', {
    'name': str,
    'email': str,
})
Commit = TypedDict('Commit', {
    'tree': str,
    'url': str,
    'message': str,
    # optional when creating a commit
    'author': Authorship,
    'committer': Authorship,
    'comments_count': int,
})
CommitLink = TypedDict('CommitLink', {
    'html_url': str,
    'sha': str,
    'url': str,
})
PrCommit = TypedDict('PrCommit', {
    'url': str,
    'sha': str,
    'commit': Commit,
    # optional when creating a commit (in which case it uses the current user)
    'author': SimpleUser,
    'committer': SimpleUser,
    'parents': List[CommitLink],
    # not actually true but we're smuggling stuff via that key
    'new_tree': str,
})


GH_LOG_PATTERN = """=> {method} {path}{qs}{body}

<= {r.status_code} {r.reason}
{headers}
{body2}
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
"""
class GH(object):
    def __init__(self, token, repo):
        self._url = 'https://api.github.com'
        self._repo = repo
        self._last_update = 0
        session = self._session = requests.Session()
        session.headers['Authorization'] = 'token {}'.format(token)
        session.headers['Accept'] = 'application/vnd.github.symmetra-preview+json'

    def _log_gh(self, logger: logging.Logger, response: requests.Response, level: int = logging.INFO, extra=None):
        """ Logs a pair of request / response to github, to the specified
        logger, at the specified level.

        Tries to format all the information (including request / response
        bodies, at least in part) so we have as much information as possible
        for post-mortems.
        """
        req = response.request
        url = werkzeug.urls.url_parse(req.url)
        if url.netloc != 'api.github.com':
            return

        body = '' if not req.body else ('\n' + pprint.pformat(json.loads(req.body.decode()), indent=4))

        body2 = ''
        if response.content:
            if _is_json(response):
                body2 = pprint.pformat(response.json(), depth=4)
            elif response.encoding is not None:
                body2 = response.text
            else: # fallback: universal decoding & replace nonprintables
                body2 = ''.join(
                    '\N{REPLACEMENT CHARACTER}' if unicodedata.category(c) == 'Cc' else c
                    for c in response.content.decode('iso-8859-1')
                )

        logger.log(level, GH_LOG_PATTERN.format(
            # requests data
            method=req.method, path=url.path, qs=url.query, body=body,
            # response data
            r=response,
            headers='\n'.join(
                '\t%s: %s' % (h, v) for h, v in response.headers.items()
            ),
            body2=utils.shorten(body2.strip(), 400)
        ), extra=extra)

    def __call__(self, method, path, params=None, json=None, check=True):
        """
        :type check: bool | dict[int:Exception]
        """
        if method.casefold() != 'get':
            to_sleep = 1. - (time.time() - self._last_update)
            if to_sleep > 0:
                time.sleep(to_sleep)

        path = f'/repos/{self._repo}/{path}'
        r = self._session.request(method, self._url + path, params=params, json=json)
        if method.casefold() != 'get':
            self._last_update = time.time() + int(r.headers.get('Retry-After', 0))

        self._log_gh(_gh, r)
        if check:
            try:
                if isinstance(check, collections.abc.Mapping):
                    exc = check.get(r.status_code)
                    if exc:
                        raise exc(r.text)
                if r.status_code >= 400:
                    raise requests.HTTPError(r.text, response=r)
            except Exception:
                self._log_gh(_logger, r, level=logging.ERROR, extra={
                    'github-request-id': r.headers.get('x-github-request-id'),
                })
                raise

        return r

    def user(self, username):
        r = self._session.get("{}/users/{}".format(self._url, username))
        r.raise_for_status()
        return r.json()

    def head(self, branch: str) -> str:
        d = utils.backoff(
            lambda: self('get', 'git/refs/heads/{}'.format(branch)).json(),
            exc=requests.HTTPError
        )

        assert d['ref'] == 'refs/heads/{}'.format(branch)
        assert d['object']['type'] == 'commit'
        _logger.debug("head(%s, %s) -> %s", self._repo, branch, d['object']['sha'])
        return d['object']['sha']

    def commit(self, sha):
        c = self('GET', 'git/commits/{}'.format(sha)).json()
        _logger.debug('commit(%s, %s) -> %s', self._repo, sha, shorten(c['message']))
        return c

    def comment(self, pr, message):
        # if the mergebot user has been blocked by the PR author, this will
        # fail, but we don't want the closing of the PR to fail, or for the
        # feedback cron to get stuck
        try:
            self('POST', 'issues/{}/comments'.format(pr), json={'body': message})
        except requests.HTTPError as r:
            if _is_json(r.response):
                body = r.response.json()
                if any(e.message == 'User is blocked' for e in (body.get('errors') or [])):
                    _logger.warning("comment(%s#%s) failed: user likely blocked", self._repo, pr)
                    return
            raise
        _logger.debug('comment(%s, %s, %s)', self._repo, pr, shorten(message))

    def close(self, pr):
        self('PATCH', 'pulls/{}'.format(pr), json={'state': 'closed'})

    def change_tags(self, pr, remove, add):
        labels_endpoint = 'issues/{}/labels'.format(pr)
        tags_before = {label['name'] for label in self('GET', labels_endpoint).json()}
        tags_after = (tags_before - remove) | add
        # replace labels entirely
        self('PUT', labels_endpoint, json={'labels': list(tags_after)})

        _logger.debug('change_tags(%s, %s, from=%s, to=%s)', self._repo, pr, tags_before, tags_after)

    def _check_updated(self, branch, to):
        """
        :return: nothing if successful, the incorrect HEAD otherwise
        """
        r = self('get', 'git/refs/heads/{}'.format(branch), check=False)
        if r.status_code == 200:
            head = r.json()['object']['sha']
        else:
            head = '<Response [%s]: %s)>' % (r.status_code, r.text)

        if head == to:
            _logger.debug("Sanity check ref update of %s to %s: ok", branch, to)
            return

        _logger.warning(
            "Sanity check ref update of %s, expected %s got %s (response-id %s)",
            branch, to, head,
            r.headers.get('x-github-request-id')
        )
        return head

    def fast_forward(self, branch, sha):
        try:
            self('patch', 'git/refs/heads/{}'.format(branch), json={'sha': sha})
            _logger.debug('fast_forward(%s, %s, %s) -> OK', self._repo, branch, sha)
            @utils.backoff(exc=exceptions.FastForwardError)
            def _wait_for_update():
                if not self._check_updated(branch, sha):
                    return
                raise exceptions.FastForwardError(self._repo) \
                    from Exception("timeout: never saw %s" % sha)
        except requests.HTTPError as e:
            _logger.debug('fast_forward(%s, %s, %s) -> ERROR', self._repo, branch, sha, exc_info=True)
            if e.response.status_code == 422:
                try:
                    r = e.response.json()
                except Exception:
                    pass
                else:
                    if isinstance(r, dict) and 'message' in r:
                        e = Exception(r['message'].lower())
            raise exceptions.FastForwardError(self._repo) from e

    def set_ref(self, branch, sha):
        # force-update ref
        r = self('patch', 'git/refs/heads/{}'.format(branch), json={
            'sha': sha,
            'force': True,
        }, check=False)

        status0 = r.status_code
        _logger.debug(
            'set_ref(%s, %s, %s -> %s (%s)',
            self._repo, branch, sha, status0,
            'OK' if status0 == 200 else r.text or r.reason
        )
        if status0 == 200:
            @utils.backoff(exc=AssertionError)
            def _wait_for_update():
                head = self._check_updated(branch, sha)
                assert not head, "Sanity check ref update of %s, expected %s got %s" % (
                    branch, sha, head
                )
            return

        # 422 makes no sense but that's what github returns, leaving 404 just
        # in case
        if status0 in (404, 422):
            # fallback: create ref
            status1 = self.create_ref(branch, sha)
            if status1 == 201:
                return
        else:
            status1 = None

        raise AssertionError("set_ref failed(%s, %s)" % (status0, status1))

    def create_ref(self, branch, sha):
        r = self('post', 'git/refs', json={
            'ref': 'refs/heads/{}'.format(branch),
            'sha': sha,
        }, check=False)
        status = r.status_code
        _logger.debug(
            'ref_create(%s, %s, %s) -> %s (%s)',
            self._repo, branch, sha, status,
            'OK' if status == 201 else r.text or r.reason
        )
        if status == 201:
            @utils.backoff(exc=AssertionError)
            def _wait_for_update():
                head = self._check_updated(branch, sha)
                assert not head, \
                    f"Sanity check ref update of {branch}, expected {sha} got {head}"
        return status

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

    def commits_lazy(self, pr: int) -> Iterable[PrCommit]:
        for page in itertools.count(1):
            r = self('get', f'pulls/{pr}/commits', params={'page': page})
            yield from r.json()
            if not r.links.get('next'):
                return

    def commits(self, pr: int) -> List[PrCommit]:
        """ Returns a PR's commits oldest first (that's what GH does &
        is what we want)
        """
        commits = list(self.commits_lazy(pr))
        # map shas to the position the commit *should* have
        idx =  {
            c: i
            for i, c in enumerate(topological_sort({
                c['sha']: [p['sha'] for p in c['parents']]
                for c in commits
            }))
        }
        return sorted(commits, key=lambda c: idx[c['sha']])

    def statuses(self, h):
        r = self('get', 'commits/{}/status'.format(h)).json()
        return [{
            'sha': r['sha'],
            **s,
        } for s in r['statuses']]

def shorten(s):
    if not s:
        return s

    line1 = s.split('\n', 1)[0]
    if len(line1) < 50:
        return line1

    return line1[:47] + '...'
