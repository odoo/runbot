""" The mergebot does not work on a dependency basis, rather all
repositories of a project are co-equal and get  (on target and
source branches).

When preparing a staging, we simply want to ensure branch-matched PRs
are staged concurrently in all repos
"""
import json

import pytest

from test_utils import re_matches, run_crons

@pytest.fixture
def repo_a(make_repo):
    return make_repo('a')

@pytest.fixture
def repo_b(make_repo):
    return make_repo('b')

@pytest.fixture
def repo_c(make_repo):
    return make_repo('c')

def make_pr(repo, prefix, trees, *, target='master', user='user', label=None,
            statuses=(('ci/runbot', 'success'), ('legal/cla', 'success')),
            reviewer='reviewer'):
    """
    :type repo: fake_github.Repo
    :type prefix: str
    :type trees: list[dict]
    :type target: str
    :type user: str
    :type label: str | None
    :type statuses: list[(str, str)]
    :type reviewer: str | None
    :rtype: fake_github.PR
    """
    base = repo.commit('heads/{}'.format(target))
    tree = repo.read_tree(base)
    c = base.id
    for i, t in enumerate(trees):
        tree.update(t)
        c = repo.make_commit(c, 'commit_{}_{:02}'.format(prefix, i), None,
                             tree=dict(tree))
    pr = repo.make_pr('title {}'.format(prefix), 'body {}'.format(prefix), target=target, ctid=c, user=user, label=label)
    for context, result in statuses:
        repo.post_status(c, result, context)
    if reviewer:
        pr.post_comment('hansen r+', reviewer)
    return pr
def to_pr(env, pr):
    return env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', pr.repo.name),
        ('number', '=', pr.number),
    ])
def make_branch(repo, name, message, tree, protect=True):
    c = repo.make_commit(None, message, None, tree=tree)
    repo.make_ref('heads/%s' % name, c)
    if protect:
        repo.protect(name)
    return c

def test_stage_one(env, project, repo_a, repo_b):
    """ First PR is non-matched from A => should not select PR from B
    """
    project.batch_limit = 1

    make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
    pr_a = make_pr(repo_a, 'A', [{'a': 'a_1'}], label='do-a-thing')

    make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})
    pr_b = make_pr(repo_b, 'B', [{'a': 'b_1'}], label='do-other-thing')

    env['runbot_merge.project']._check_progress()

    assert to_pr(env, pr_a).state == 'ready'
    assert to_pr(env, pr_a).staging_id
    assert to_pr(env, pr_b).state == 'ready'
    assert not to_pr(env, pr_b).staging_id

def test_stage_match(env, project, repo_a, repo_b):
    """ First PR is matched from A,  => should select matched PR from B
    """
    project.batch_limit = 1
    make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
    pr_a = make_pr(repo_a, 'A', [{'a': 'a_1'}], label='do-a-thing')

    make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})
    pr_b = make_pr(repo_b, 'B', [{'a': 'b_1'}], label='do-a-thing')

    env['runbot_merge.project']._check_progress()

    pr_a = to_pr(env, pr_a)
    pr_b = to_pr(env, pr_b)
    assert pr_a.state == 'ready'
    assert pr_a.staging_id
    assert pr_b.state == 'ready'
    assert pr_b.staging_id
    # should be part of the same staging
    assert pr_a.staging_id == pr_b.staging_id, \
        "branch-matched PRs should be part of the same staging"

def test_unmatch_patch(env, project, repo_a, repo_b):
    """ When editing files via the UI for a project you don't have write
    access to, a branch called patch-XXX is automatically created in your
    profile to hold the change.

    This means it's possible to create a:patch-1 and b:patch-1 without
    intending them to be related in any way, and more likely than the opposite
    since there is no user control over the branch names (save by actually
    creating/renaming branches afterwards before creating the PR).

    -> PRs with a branch name of patch-* should not be label-matched
    """
    project.batch_limit = 1
    make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
    pr_a = make_pr(repo_a, 'A', [{'a': 'a_1'}], label='patch-1')

    make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})
    pr_b = make_pr(repo_b, 'B', [{'a': 'b_1'}], label='patch-1')

    env['runbot_merge.project']._check_progress()

    pr_a = to_pr(env, pr_a)
    pr_b = to_pr(env, pr_b)
    assert pr_a.state == 'ready'
    assert pr_a.staging_id
    assert pr_b.state == 'ready'
    assert not pr_b.staging_id, 'patch-* PRs should not be branch-matched'

def test_sub_match(env, project, repo_a, repo_b, repo_c):
    """ Branch-matching should work on a subset of repositories
    """
    project.batch_limit = 1
    make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
    # no pr here

    make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})
    pr_b = make_pr(repo_b, 'B', [{'a': 'b_1'}], label='do-a-thing')

    make_branch(repo_c, 'master', 'initial', {'a': 'c_0'})
    pr_c = make_pr(repo_c, 'C', [{'a': 'c_1'}], label='do-a-thing')

    env['runbot_merge.project']._check_progress()

    pr_b = to_pr(env, pr_b)
    pr_c = to_pr(env, pr_c)
    assert pr_b.state == 'ready'
    assert pr_b.staging_id
    assert pr_c.state == 'ready'
    assert pr_c.staging_id
    # should be part of the same staging
    assert pr_c.staging_id == pr_b.staging_id, \
        "branch-matched PRs should be part of the same staging"

    st = pr_b.staging_id
    b_staging = repo_b.commit('heads/staging.master')
    c_staging = repo_c.commit('heads/staging.master')
    assert json.loads(st.heads) == {
        repo_a.name: repo_a.commit('heads/staging.master').id,
        repo_a.name + '^': repo_a.commit('heads/master').id,
        repo_b.name: b_staging.id,
        repo_b.name + '^': b_staging.parents[0],
        repo_c.name: c_staging.id,
        repo_c.name + '^': c_staging.parents[0],
    }

def test_merge_fail(env, project, repo_a, repo_b, users):
    """ In a matched-branch scenario, if merging in one of the linked repos
    fails it should revert the corresponding merges
    """
    project.batch_limit = 1

    make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
    make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})

    # first set of matched PRs
    pr1a = make_pr(repo_a, 'A', [{'a': 'a_1'}], label='do-a-thing')
    pr1b = make_pr(repo_b, 'B', [{'a': 'b_1'}], label='do-a-thing')

    # add a conflicting commit to B so the staging fails
    repo_b.make_commit('heads/master', 'cn', None, tree={'a': 'cn'})

    # and a second set of PRs which should get staged while the first set
    # fails
    pr2a = make_pr(repo_a, 'A2', [{'b': 'ok'}], label='do-b-thing')
    pr2b = make_pr(repo_b, 'B2', [{'b': 'ok'}], label='do-b-thing')

    run_crons(env)

    s2 = to_pr(env, pr2a) | to_pr(env, pr2b)
    st = env['runbot_merge.stagings'].search([])
    assert set(st.batch_ids.prs.ids) == set(s2.ids)

    failed = to_pr(env, pr1b)
    assert failed.state == 'error'
    assert pr1b.comments == [
        (users['reviewer'], 'hansen r+'),
        (users['user'], re_matches('^Unable to stage PR')),
    ]
    other = to_pr(env, pr1a)
    assert not other.staging_id
    assert [
        c['commit']['message']
        for c in repo_a.log('heads/staging.master')
    ] == [
        re_matches('^force rebuild'),
        'commit_A2_00\n\ncloses %s#2\n\nSigned-off-by: Reviewer <reviewer@example.com>' % repo_a.name,
        'initial'
    ], "dummy commit + squash-merged PR commit + root commit"

def test_ff_fail(env, project, repo_a, repo_b):
    """ In a matched-branch scenario, fast-forwarding one of the repos fails
    the entire thing should be rolled back
    """
    project.batch_limit = 1
    root_a = make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
    make_pr(repo_a, 'A', [{'a': 'a_1'}], label='do-a-thing')

    make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})
    make_pr(repo_b, 'B', [{'a': 'b_1'}], label='do-a-thing')

    env['runbot_merge.project']._check_progress()

    # add second commit blocking FF
    cn = repo_b.make_commit('heads/master', 'second', None, tree={'a': 'b_0', 'b': 'other'})
    assert repo_b.commit('heads/master').id == cn

    repo_a.post_status('heads/staging.master', 'success', 'ci/runbot')
    repo_a.post_status('heads/staging.master', 'success', 'legal/cla')
    repo_b.post_status('heads/staging.master', 'success', 'ci/runbot')
    repo_b.post_status('heads/staging.master', 'success', 'legal/cla')

    env['runbot_merge.project']._check_progress()
    assert repo_b.commit('heads/master').id == cn,\
        "B should still be at the conflicting commit"
    assert repo_a.commit('heads/master').id == root_a,\
        "FF A should have been rolled back when B failed"

    # should be re-staged
    st = env['runbot_merge.stagings'].search([])
    assert len(st) == 1
    assert len(st.batch_ids.prs) == 2

class TestCompanionsNotReady:
    def test_one_pair(self, env, project, repo_a, repo_b, owner, users):
        """ If the companion of a ready branch-matched PR is not ready,
        they should not get staged
        """
        project.batch_limit = 1
        make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
        # pr_a is born ready
        p_a = make_pr(repo_a, 'A', [{'a': 'a_1'}], label='do-a-thing')

        make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})
        p_b = make_pr(repo_b, 'B', [{'a': 'b_1'}], label='do-a-thing', reviewer=None)

        pr_a = to_pr(env, p_a)
        pr_b = to_pr(env, p_b)
        assert pr_a.state == 'ready'
        assert pr_b.state == 'validated'
        assert pr_a.label == pr_b.label == '{}:do-a-thing'.format(owner)

        run_crons(env)

        assert not pr_b.staging_id
        assert not pr_a.staging_id, \
            "pr_a should not have been staged as companion is not ready"

        assert p_a.comments == [
            (users['reviewer'], 'hansen r+'),
            (users['user'], "Linked pull request(s) %s#%d not ready. Linked PRs are not staged until all of them are ready." % (repo_b.name, p_b.number)),
        ]
        # ensure the message is only sent once per PR
        env['runbot_merge.pull_requests']._check_linked_prs_statuses()
        assert p_a.comments == [
            (users['reviewer'], 'hansen r+'),
            (users['user'], "Linked pull request(s) %s#%d not ready. Linked PRs are not staged until all of them are ready." % (repo_b.name, p_b.number)),
        ]
        assert p_b.comments == []

    def test_two_of_three_unready(self, env, project, repo_a, repo_b, repo_c, owner, users):
        """ In a 3-batch, if two of the PRs are not ready both should be
        linked by the first one
        """
        project.batch_limit = 1
        make_branch(repo_a, 'master', 'initial', {'f': 'a0'})
        pr_a = make_pr(repo_a, 'A', [{'f': 'a1'}], label='a-thing', reviewer=None)

        make_branch(repo_b, 'master', 'initial', {'f': 'b0'})
        pr_b = make_pr(repo_b, 'B', [{'f': 'b1'}], label='a-thing')

        make_branch(repo_c, 'master', 'initial', {'f': 'c0'})
        pr_c = make_pr(repo_c, 'C', [{'f': 'c1'}], label='a-thing', reviewer=None)

        run_crons(env)
        assert pr_a.comments == []
        assert pr_b.comments == [
            (users['reviewer'], 'hansen r+'),
            (users['user'], "Linked pull request(s) %s#%d, %s#%d not ready. Linked PRs are not staged until all of them are ready." % (
                repo_a.name, pr_a.number,
                repo_c.name, pr_c.number
            ))
        ]
        assert pr_c.comments == []

    def test_one_of_three_unready(self, env, project, repo_a, repo_b, repo_c, owner, users):
        """ In a 3-batch, if one PR is not ready it should be linked on the
        other two
        """
        project.batch_limit = 1
        make_branch(repo_a, 'master', 'initial', {'f': 'a0'})
        pr_a = make_pr(repo_a, 'A', [{'f': 'a1'}], label='a-thing', reviewer=None)

        make_branch(repo_b, 'master', 'initial', {'f': 'b0'})
        pr_b = make_pr(repo_b, 'B', [{'f': 'b1'}], label='a-thing')

        make_branch(repo_c, 'master', 'initial', {'f': 'c0'})
        pr_c = make_pr(repo_c, 'C', [{'f': 'c1'}], label='a-thing')

        run_crons(env)
        assert pr_a.comments == []
        assert pr_b.comments == [
            (users['reviewer'], 'hansen r+'),
            (users['user'], "Linked pull request(s) %s#%d not ready. Linked PRs are not staged until all of them are ready." % (
                repo_a.name, pr_a.number
            ))
        ]
        assert pr_c.comments == [
            (users['reviewer'], 'hansen r+'),
            (users['user'],
             "Linked pull request(s) %s#%d not ready. Linked PRs are not staged until all of them are ready." % (
                 repo_a.name, pr_a.number
             ))
        ]

def test_other_failed(env, project, repo_a, repo_b, owner, users):
    """ In a non-matched-branch scenario, if the companion staging (copy of
    targets) fails when built with the PR, it should provide a non-useless
    message
    """
    make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
    # pr_a is born ready
    pr_a = make_pr(repo_a, 'A', [{'a': 'a_1'}], label='do-a-thing')

    make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})

    env['runbot_merge.project']._check_progress()
    pr = to_pr(env, pr_a)
    assert pr.staging_id

    repo_a.post_status('heads/staging.master', 'success', 'legal/cla')
    repo_a.post_status('heads/staging.master', 'success', 'ci/runbot', target_url="http://example.org/a")
    repo_b.post_status('heads/staging.master', 'success', 'legal/cla')
    repo_b.post_status('heads/staging.master', 'failure', 'ci/runbot', target_url="http://example.org/b")
    run_crons(env)

    sth = repo_b.commit('heads/staging.master').id
    assert not pr.staging_id
    assert pr.state == 'error'
    assert pr_a.comments == [
        (users['reviewer'], 'hansen r+'),
        (users['user'], 'Staging failed: ci/runbot on %s (view more at http://example.org/b)' % sth)
    ]

class TestMultiBatches:
    def test_batching(self, env, project, repo_a, repo_b):
        """ If multiple batches (label groups) are ready they should get batched
        together (within the limits of teh project's batch limit)
        """
        project.batch_limit = 3
        make_branch(repo_a, 'master', 'initial', {'a': 'a0'})
        make_branch(repo_b, 'master', 'initial', {'b': 'b0'})

        prs = [(
            a and to_pr(env, make_pr(repo_a, 'A{}'.format(i), [{'a{}'.format(i): 'a{}'.format(i)}], label='batch{}'.format(i))),
            b and to_pr(env, make_pr(repo_b, 'B{}'.format(i), [{'b{}'.format(i): 'b{}'.format(i)}], label='batch{}'.format(i)))
        )
            for i, (a, b) in enumerate([(1, 1), (0, 1), (1, 1), (1, 1), (1, 0)])
        ]

        env['runbot_merge.project']._check_progress()

        st = env['runbot_merge.stagings'].search([])
        assert st
        assert len(st.batch_ids) == 3,\
            "Should have batched the first <batch_limit> batches"
        assert st.mapped('batch_ids.prs') == (
            prs[0][0] | prs[0][1]
          | prs[1][1]
          | prs[2][0] | prs[2][1]
        )

        assert not prs[3][0].staging_id
        assert not prs[3][1].staging_id
        assert not prs[4][0].staging_id

    def test_batching_split(self, env, repo_a, repo_b):
        """ If a staging fails, it should get split properly across repos
        """
        make_branch(repo_a, 'master', 'initial', {'a': 'a0'})
        make_branch(repo_b, 'master', 'initial', {'b': 'b0'})

        prs = [(
            a and to_pr(env, make_pr(repo_a, 'A{}'.format(i), [{'a{}'.format(i): 'a{}'.format(i)}], label='batch{}'.format(i))),
            b and to_pr(env, make_pr(repo_b, 'B{}'.format(i), [{'b{}'.format(i): 'b{}'.format(i)}], label='batch{}'.format(i)))
        )
            for i, (a, b) in enumerate([(1, 1), (0, 1), (1, 1), (1, 1), (1, 0)])
        ]

        env['runbot_merge.project']._check_progress()

        st0 = env['runbot_merge.stagings'].search([])
        assert len(st0.batch_ids) == 5
        assert len(st0.mapped('batch_ids.prs')) == 8

        # mark b.staging as failed -> should create two splits with (0, 1)
        # and (2, 3, 4) and stage the first one
        repo_b.post_status('heads/staging.master', 'success', 'legal/cla')
        repo_b.post_status('heads/staging.master', 'failure', 'ci/runbot')

        env['runbot_merge.project']._check_progress()

        assert not st0.active

        # at this point we have a re-staged split and an unstaged split
        st = env['runbot_merge.stagings'].search([])
        sp = env['runbot_merge.split'].search([])
        assert st
        assert sp

        assert len(st.batch_ids) == 2
        assert st.mapped('batch_ids.prs') == \
            prs[0][0] | prs[0][1] | prs[1][1]

        assert len(sp.batch_ids) == 3
        assert sp.mapped('batch_ids.prs') == \
            prs[2][0] | prs[2][1] | prs[3][0] | prs[3][1] | prs[4][0]

def test_urgent(env, repo_a, repo_b):
    """ Either PR of a co-dependent pair being p=0 leads to the entire pair
    being prioritized
    """
    make_branch(repo_a, 'master', 'initial', {'a0': 'a'})
    make_branch(repo_b, 'master', 'initial', {'b0': 'b'})

    pr_a = make_pr(repo_a, 'A', [{'a1': 'a'}, {'a2': 'a'}], label='batch', reviewer=None, statuses=[])
    pr_b = make_pr(repo_b, 'B', [{'b1': 'b'}, {'b2': 'b'}], label='batch', reviewer=None, statuses=[])
    pr_c = make_pr(repo_a, 'C', [{'c1': 'c', 'c2': 'c'}])

    pr_a.post_comment('hansen rebase-merge', 'reviewer')
    pr_b.post_comment('hansen rebase-merge p=0', 'reviewer')

    env['runbot_merge.project']._check_progress()
    # should have batched pr_a and pr_b despite neither being reviewed or
    # approved
    p_a, p_b = to_pr(env, pr_a), to_pr(env, pr_b)
    p_c = to_pr(env, pr_c)
    assert p_a.batch_id and p_b.batch_id and p_a.batch_id == p_b.batch_id,\
        "a and b should have been recognised as co-dependent"
    assert not p_c.staging_id
