# -*- coding: utf-8 -*-
# target branch '-' source branch '-' base32 unique '-forwardport'
import itertools
import re

MESSAGE_TEMPLATE = """{message}

closes {repo}#{number}

{headers}Signed-off-by: {name} <{login}@users.noreply.github.com>"""
REF_PATTERN = r'{target}-{source}-\w{{8}}-forwardport'

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
