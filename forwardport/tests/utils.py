# -*- coding: utf-8 -*-
import itertools
import re

MESSAGE_TEMPLATE = """{message}

closes {repo}#{number}

{headers}Signed-off-by: {name} <{login}@users.noreply.github.com>"""
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

class re_matches:
    def __init__(self, pattern, flags=0):
        self._r = re.compile(pattern, flags)

    def __eq__(self, text):
        return self._r.match(text)

    def __repr__(self):
        return '~' + self._r.pattern + '~'


def make_basic(env, config, make_repo, *, reponame='proj', project_name='myproject'):
    """ Creates a basic repo with 3 forking branches

    0 -- 1 -- 2 -- 3 -- 4  : a
              |
              `-- 11 -- 22 : b
                  |
                  `-- 111  : c
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
            'required_statuses': 'legal/cla,ci/runbot',
            'branch_ids': [
                (0, 0, {'name': 'a', 'fp_sequence': 2, 'fp_target': True}),
                (0, 0, {'name': 'b', 'fp_sequence': 1, 'fp_target': True}),
                (0, 0, {'name': 'c', 'fp_sequence': 0, 'fp_target': True}),
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
    project.write({
        'repo_ids': [(0, 0, {
            'name': prod.name,
            'fp_remote_target': other.name,
        })],
    })

    return prod, other
