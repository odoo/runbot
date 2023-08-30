# -*- coding: utf-8 -*-
import itertools
import re

from lxml import html

MESSAGE_TEMPLATE = """{message}

closes {repo}#{number}

{headers}Signed-off-by: {name} <{email}>
"""
# target branch '-' source branch '-' base64 unique '-fw'
REF_PATTERN = r'{target}-{source}-[a-zA-Z0-9_-]{{4}}-fw'

class Commit:
    def __init__(self, message, *, author=None, committer=None, tree, reset=False):
        self.id = None
        self.message = message
        self.author = author
        self.committer = committer
        self.tree = tree
        self.reset = reset

def validate_all(repos, refs, contexts=('ci/runbot', 'legal/cla')):
    """ Post a "success" status for each context on each ref of each repo
    """
    for repo, branch, context in itertools.product(repos, refs, contexts):
        repo.post_status(branch, 'success', context)

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
    prx = repo.make_pr(title='title', body='body', target='master', head=c2)
    return prx

class re_matches:
    def __init__(self, pattern, flags=0):
        self._r = re.compile(pattern, flags)

    def __eq__(self, text):
        return self._r.match(text)

    def __repr__(self):
        return repr(self._r.pattern)

def seen(env, pr, users):
    return users['user'], f'[Pull request status dashboard]({to_pr(env, pr).url}).'

def make_basic(env, config, make_repo, *, reponame='proj', project_name='myproject'):
    """ Creates a basic repo with 3 forking branches

    f = 0 -- 1 -- 2 -- 3 -- 4  : a
                  |
    g =           `-- 11 -- 22 : b
                     |
    h =               `-- 111  : c
    each branch just adds and modifies a file (resp. f, g and h) through the
    contents sequence a b c d e
    """
    Projects = env['runbot_merge.project']
    project = Projects.search([('name', '=', project_name)])
    if not project:
        project = env['runbot_merge.project'].create({
            'name': project_name,
            'github_token': config['github']['token'],
            'github_prefix': 'hansen',
            'fp_github_token': config['github']['token'],
            'fp_github_name': 'herbert',
            'fp_github_email': 'hb@example.com',
            'branch_ids': [
                (0, 0, {'name': 'a', 'sequence': 100}),
                (0, 0, {'name': 'b', 'sequence': 80}),
                (0, 0, {'name': 'c', 'sequence': 60}),
            ],
        })

    prod = make_repo(reponame)
    with prod:
        a_0, a_1, a_2, a_3, a_4, = prod.make_commits(
            None,
            Commit("0", tree={'f': 'a'}),
            Commit("1", tree={'f': 'b'}),
            Commit("2", tree={'f': 'c'}),
            Commit("3", tree={'f': 'd'}),
            Commit("4", tree={'f': 'e'}),
            ref='heads/a',
        )
        b_1, b_2 = prod.make_commits(
            a_2,
            Commit('11', tree={'g': 'a'}),
            Commit('22', tree={'g': 'b'}),
            ref='heads/b',
        )
        prod.make_commits(
            b_1,
            Commit('111', tree={'h': 'a'}),
            ref='heads/c',
        )
    other = prod.fork()
    repo = env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': prod.name,
        'required_statuses': 'legal/cla,ci/runbot',
        'fp_remote_target': other.name,
    })
    env['res.partner'].search([
        ('github_login', '=', config['role_reviewer']['user'])
    ]).write({
        'review_rights': [(0, 0, {'repository_id': repo.id, 'review': True})]
    })
    env['res.partner'].search([
        ('github_login', '=', config['role_self_reviewer']['user'])
    ]).write({
        'review_rights': [(0, 0, {'repository_id': repo.id, 'self_review': True})]
    })

    return prod, other

def pr_page(page, pr):
    return html.fromstring(page(f'/{pr.repo.name}/pull/{pr.number}'))

def to_pr(env, pr):
    pr = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', pr.repo.name),
        ('number', '=', pr.number),
    ])
    assert len(pr) == 1, f"Expected to find {pr.repo.name}#{pr.number}, got {pr}."
    return pr

def part_of(label, pr_id, *, separator='\n\n'):
    return f'{label}{separator}Part-of: {pr_id.display_name}\n'
