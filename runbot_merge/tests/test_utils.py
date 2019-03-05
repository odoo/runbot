# -*- coding: utf-8 -*-
import re


class re_matches:
    def __init__(self, pattern, flags=0):
        self._r = re.compile(pattern, flags)

    def __eq__(self, text):
        return self._r.match(text)

    def __repr__(self):
        return '~' + self._r.pattern + '~'

def run_crons(env):
    "Helper to run all crons (in a relevant order) except for the fetch PR one"
    env['runbot_merge.commit']._notify()
    env['runbot_merge.project']._check_progress()
    env['runbot_merge.pull_requests']._check_linked_prs_statuses()
    env['runbot_merge.project']._send_feedback()

def get_partner(env, gh_login):
    return env['res.partner'].search([('github_login', '=', gh_login)])

def _simple_init(repo):
    """ Creates a very simple initialisation: a master branch with a commit,
    and a PR by 'user' with two commits, targeted to the master branch
    """
    m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
    repo.make_ref('heads/master', m)
    c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
    c2 = repo.make_commit(c1, 'second', None, tree={'m': 'c2'})
    prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
    return prx
