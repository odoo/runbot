import collections
import itertools
import json as json_
import logging
import logging.handlers
import os
import pathlib
import pprint
import textwrap
import unicodedata

import requests
import werkzeug.urls

import odoo.netsvc
from odoo.tools import topological_sort, config
from . import exceptions, utils


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

GH_LOG_PATTERN = """=> {method} /{self._repo}/{path}{qs}{body}

<= {r.status_code} {r.reason}
{headers}
{body2}
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
"""
class GH(object):
    def __init__(self, token, repo):
        self._url = 'https://api.github.com'
        self._repo = repo
        session = self._session = requests.Session()
        session.headers['Authorization'] = 'token {}'.format(token)
        session.headers['Accept'] = 'application/vnd.github.symmetra-preview+json'

    def _log_gh(self, logger, method, path, params, json, response, level=logging.INFO):
        """ Logs a pair of request / response to github, to the specified
        logger, at the specified level.

        Tries to format all the information (including request / response
        bodies, at least in part) so we have as much information as possible
        for post-mortems.
        """
        body = body2 = ''

        if json:
            body = '\n' + textwrap.indent('\t', pprint.pformat(json, indent=4))

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
            self=self,
            # requests data
            method=method, path=path,
            qs='' if not params else ('?' + werkzeug.urls.url_encode(params)),
            body=utils.shorten(body.strip(), 400),
            # response data
            r=response,
            headers='\n'.join(
                '\t%s: %s' % (h, v) for h, v in response.headers.items()
            ),
            body2=utils.shorten(body2.strip(), 400)
        ))
        return body2

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
        self._log_gh(_gh, method, path, params, json, r)
        if check:
            if isinstance(check, collections.Mapping):
                exc = check.get(r.status_code)
                if exc:
                    raise exc(r.text)
            if r.status_code >= 400:
                body = self._log_gh(
                    _logger, method, path, params, json, r, level=logging.ERROR)
                if not isinstance(body, (bytes, str)):
                    raise requests.HTTPError(
                        json_.dumps(body, indent=4),
                        response=r
                    )
            r.raise_for_status()
        return r

    def user(self, username):
        r = self._session.get("{}/users/{}".format(self._url, username))
        r.raise_for_status()
        return r.json()

    def head(self, branch):
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
                    _logger.warning("comment(%s:%s) failed: user likely blocked", self._repo, pr)
                    return
            raise
        _logger.debug('comment(%s, %s, %s)', self._repo, pr, shorten(message))

    def close(self, pr):
        self('PATCH', 'pulls/{}'.format(pr), json={'state': 'closed'})

    def change_tags(self, pr, to_):
        labels_endpoint = 'issues/{}/labels'.format(pr)
        from .models.pull_requests import _TAGS
        mergebot_tags = set.union(*_TAGS.values())
        tags_before = {label['name'] for label in self('GET', labels_endpoint).json()}
        # remove all mergebot tags from the PR, then add just the ones which should be set
        tags_after = (tags_before - mergebot_tags) | to_
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
            head = '<Response [%s]: %s)>' % (r.status_code, r.json() if _is_json(r) else r.text)

        if head == to:
            _logger.info("Sanity check ref update of %s to %s: ok", branch, to)
            return

        _logger.warning("Sanity check ref update of %s, expected %s got %s", branch, to, head)
        return head

    def fast_forward(self, branch, sha):
        try:
            self('patch', 'git/refs/heads/{}'.format(branch), json={'sha': sha})
            _logger.debug('fast_forward(%s, %s, %s) -> OK', self._repo, branch, sha)
            @utils.backoff(exc=exceptions.FastForwardError)
            def _wait_for_update():
                if not self._check_updated(branch, sha):
                    return
                raise exceptions.FastForwardError(self._repo)
        except requests.HTTPError:
            _logger.debug('fast_forward(%s, %s, %s) -> ERROR', self._repo, branch, sha, exc_info=True)
            raise exceptions.FastForwardError(self._repo)

    def set_ref(self, branch, sha):
        # force-update ref
        r = self('patch', 'git/refs/heads/{}'.format(branch), json={
            'sha': sha,
            'force': True,
        }, check=False)

        status0 = r.status_code
        _logger.debug(
            'set_ref(update, %s, %s, %s -> %s (%s)',
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
        status1 = None
        if status0 in (404, 422):
            # fallback: create ref
            r = self('post', 'git/refs', json={
                'ref': 'refs/heads/{}'.format(branch),
                'sha': sha,
            }, check=False)
            status1 = r.status_code
            _logger.debug(
                'set_ref(create, %s, %s, %s) -> %s (%s)',
                self._repo, branch, sha, status1,
                'OK' if status1 == 201 else r.text or r.reason
            )
            if status1 == 201:
                @utils.backoff(exc=AssertionError)
                def _wait_for_update():
                    head = self._check_updated(branch, sha)
                    assert not head, "Sanity check ref update of %s, expected %s got %s" % (
                        branch, sha, head
                    )
                return

        raise AssertionError("set_ref failed(%s, %s)" % (status0, status1))

    def merge(self, sha, dest, message):
        r = self('post', 'merges', json={
            'base': dest,
            'head': sha,
            'commit_message': message,
        }, check={409: exceptions.MergeError})
        try:
            r = r.json()
        except Exception:
            raise exceptions.MergeError("Got non-JSON reponse from github: %s %s (%s)" % (r.status_code, r.reason, r.text))
        _logger.debug(
            "merge(%s, %s (%s), %s) -> %s",
            self._repo, dest, r['parents'][0]['sha'],
            shorten(message), r['sha']
        )
        return dict(r['commit'], sha=r['sha'], parents=r['parents'])

    def rebase(self, pr, dest, reset=False, commits=None):
        """ Rebase pr's commits on top of dest, updates dest unless ``reset``
        is set.

        Returns the hash of the rebased head and a map of all PR commits (to the PR they were rebased to)
        """
        logger = _logger.getChild('rebase')
        original_head = self.head(dest)
        if commits is None:
            commits = self.commits(pr)

        logger.debug("rebasing %s, %s on %s (reset=%s, commits=%s)",
                     self._repo, pr, dest, reset, len(commits))

        assert commits, "can't rebase a PR with no commits"
        prev = original_head
        for original in commits:
            assert len(original['parents']) == 1, "can't rebase commits with more than one parent"
            tmp_msg = 'temp rebasing PR %s (%s)' % (pr, original['sha'])
            merged = self.merge(original['sha'], dest, tmp_msg)

            # whichever parent is not original['sha'] should be what dest
            # deref'd to, and we want to check that matches the "left parent" we
            # expect (either original_head or the previously merged commit)
            [base_commit] = (parent['sha'] for parent in merged['parents']
                             if parent['sha'] != original['sha'])
            assert prev == base_commit,\
                "Inconsistent view of %s between head (%s) and merge (%s)" % (
                    dest, prev, base_commit,
                )
            prev = merged['sha']
            original['new_tree'] = merged['tree']['sha']

        prev = original_head
        mapping = {}
        for c in commits:
            copy = self('post', 'git/commits', json={
                'message': c['commit']['message'],
                'tree': c['new_tree'],
                'parents': [prev],
                'author': c['commit']['author'],
                'committer': c['commit']['committer'],
            }, check={409: exceptions.MergeError}).json()
            logger.debug('copied %s to %s (parent: %s)', c['sha'], copy['sha'], prev)
            prev = mapping[c['sha']] = copy['sha']

        if reset:
            self.set_ref(dest, original_head)
        else:
            self.set_ref(dest, prev)

        logger.debug('rebased %s, %s on %s (reset=%s, commits=%s) -> %s',
                      self._repo, pr, dest, reset, len(commits),
                      prev)
        # prev is updated after each copy so it's the rebased PR head
        return prev, mapping

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

    def commits_lazy(self, pr):
        for page in itertools.count(1):
            r = self('get', 'pulls/{}/commits'.format(pr), params={'page': page})
            yield from r.json()
            if not r.links.get('next'):
                return

    def commits(self, pr):
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
