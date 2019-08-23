# -*- coding: utf-8 -*-
# target branch '-' source branch '-' base32 unique '-forwardport'
import itertools

MESSAGE_TEMPLATE = """{message}

closes {repo}#{number}

{headers}Signed-off-by: {name} <{login}@users.noreply.github.com>"""
REF_PATTERN = r'{target}-{source}-\w{{8}}-forwardport'

class Commit:
    def __init__(self, message, *, author=None, committer=None, tree):
        self.id = None
        self.message = message
        self.author = author
        self.committer = committer
        self.tree = tree

def validate_all(repos, refs, contexts=('ci/runbot', 'legal/cla')):
    """ Post a "success" status for each context on each ref of each repo
    """
    for repo, branch, context in itertools.product(repos, refs, contexts):
        repo.post_status(branch, 'success', context)
