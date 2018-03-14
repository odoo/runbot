""" The mergebot does not work on a dependency basis, rather all
repositories of a project are co-equal and get  (on target and
source branches).

When preparing a staging, we simply want to ensure branch-matched PRs
are staged concurrently in all repos
"""
import json

import odoo

import pytest

from fake_github import git

@pytest.fixture
def project(env):
    env['res.partner'].create({
        'name': "Reviewer",
        'github_login': 'reviewer',
        'reviewer': True,
    })
    env['res.partner'].create({
        'name': "Self Reviewer",
        'github_login': 'self-reviewer',
        'self_reviewer': True,
    })
    return env['runbot_merge.project'].create({
        'name': 'odoo',
        'github_token': 'okokok',
        'github_prefix': 'hansen',
        'branch_ids': [(0, 0, {'name': 'master'})],
        'required_statuses': 'legal/cla,ci/runbot',
    })

@pytest.fixture
def repo_a(gh, project):
    project.write({'repo_ids': [(0, 0, {'name': "odoo/a"})]})
    return gh.repo('odoo/a', hooks=[
        ((odoo.http.root, '/runbot_merge/hooks'), ['pull_request', 'issue_comment', 'status'])
    ])

@pytest.fixture
def repo_b(gh, project):
    project.write({'repo_ids': [(0, 0, {'name': "odoo/b"})]})
    return gh.repo('odoo/b', hooks=[
        ((odoo.http.root, '/runbot_merge/hooks'), ['pull_request', 'issue_comment', 'status'])
    ])

@pytest.fixture
def repo_c(gh, project):
    project.write({'repo_ids': [(0, 0, {'name': "odoo/c"})]})
    return gh.repo('odoo/c', hooks=[
        ((odoo.http.root, '/runbot_merge/hooks'), ['pull_request', 'issue_comment', 'status'])
    ])

def make_pr(repo, prefix, trees, target='master', user='user', label=None):
    base = repo.commit(f'heads/{target}')
    tree = dict(repo.objects[base.tree])
    c = base.id
    for i, t in enumerate(trees):
        tree.update(t)
        c = repo.make_commit(c, f'commit_{prefix}_{i:02}', None,
                             tree=dict(tree))
    pr = repo.make_pr(f'title {prefix}', f'body {prefix}', target=target,
                      ctid=c, user=user, label=label and f'{user}:{label}')
    repo.post_status(c, 'success', 'ci/runbot')
    repo.post_status(c, 'success', 'legal/cla')
    pr.post_comment('hansen r+', 'reviewer')
    return pr
def to_pr(env, pr):
    return env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', pr.repo.name),
        ('number', '=', pr.number),
    ])
def test_stage_one(env, project, repo_a, repo_b):
    """ First PR is non-matched from A => should not select PR from B
    """
    project.batch_limit = 1

    repo_a.make_ref(
        'heads/master',
        repo_a.make_commit(None, 'initial', None, tree={'a': 'a_0'})
    )
    pr_a = make_pr(repo_a, 'A', [{'a': 'a_1'}], label='do-a-thing')

    repo_b.make_ref(
        'heads/master',
        repo_b.make_commit(None, 'initial', None, tree={'a': 'b_0'})
    )
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
    repo_a.make_ref(
        'heads/master',
        repo_a.make_commit(None, 'initial', None, tree={'a': 'a_0'})
    )
    pr_a = make_pr(repo_a, 'A', [{'a': 'a_1'}], label='do-a-thing')

    repo_b.make_ref(
        'heads/master',
        repo_b.make_commit(None, 'initial', None, tree={'a': 'b_0'})
    )
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

def test_sub_match(env, project, repo_a, repo_b, repo_c):
    """ Branch-matching should work on a subset of repositories
    """
    project.batch_limit = 1
    repo_a.make_ref(
        'heads/master',
        repo_a.make_commit(None, 'initial', None, tree={'a': 'a_0'})
    )
    # no pr here

    repo_b.make_ref(
        'heads/master',
        repo_b.make_commit(None, 'initial', None, tree={'a': 'b_0'})
    )
    pr_b = make_pr(repo_b, 'B', [{'a': 'b_1'}], label='do-a-thing')

    repo_c.make_ref(
        'heads/master',
        repo_c.make_commit(None, 'initial', None, tree={'a': 'c_0'})
    )
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
    assert json.loads(st.heads) == {
        'odoo/a': repo_a.commit('heads/master').id,
        'odoo/b': repo_b.commit('heads/staging.master').id,
        'odoo/c': repo_c.commit('heads/staging.master').id,
    }

def test_merge_fail(env, project, repo_a, repo_b):
    """ In a matched-branch scenario, if merging in one of the linked repos
    fails it should revert the corresponding merges
    """
    project.batch_limit = 1

    root_a = repo_a.make_commit(None, 'initial', None, tree={'a': 'a_0'})
    repo_a.make_ref('heads/master', root_a)
    root_b = repo_b.make_commit(None, 'initial', None, tree={'a': 'b_0'})
    repo_b.make_ref('heads/master', root_b)

    # first set of matched PRs
    pr1a = make_pr(repo_a, 'A', [{'a': 'a_1'}], label='do-a-thing')
    pr1b = make_pr(repo_b, 'B', [{'a': 'b_1'}], label='do-a-thing')

    # add a conflicting commit to B so the staging fails
    repo_b.make_commit('heads/master', 'cn', None, tree={'a': 'cn'})

    # and a second set of PRs which should get staged while the first set
    # fails
    pr2a = make_pr(repo_a, 'A2', [{'b': 'ok'}], label='do-b-thing')
    pr2b = make_pr(repo_b, 'B2', [{'b': 'ok'}], label='do-b-thing')

    env['runbot_merge.project']._check_progress()

    s2 = to_pr(env, pr2a) | to_pr(env, pr2b)
    st = env['runbot_merge.stagings'].search([])
    assert st
    assert st.batch_ids.prs == s2

    failed = to_pr(env, pr1b)
    assert failed.state == 'error'
    assert pr1b.comments == [
        ('reviewer', 'hansen r+'),
        ('<insert current user here>', 'Unable to stage PR (merge conflict)'),
    ]
    other = to_pr(env, pr1a)
    assert not other.staging_id
    assert len(list(repo_a.log('heads/staging.master'))) == 2,\
        "root commit + squash-merged PR commit"

def test_ff_fail(env, project, repo_a, repo_b):
    """ In a matched-branch scenario, fast-forwarding one of the repos fails
    the entire thing should be rolled back
    """
    project.batch_limit = 1
    root_a = repo_a.make_commit(None, 'initial', None, tree={'a': 'a_0'})
    repo_a.make_ref('heads/master', root_a)
    make_pr(repo_a, 'A', [{'a': 'a_1'}], label='do-a-thing')

    root_b = repo_b.make_commit(None, 'initial', None, tree={'a': 'b_0'})
    repo_b.make_ref('heads/master', root_b)
    make_pr(repo_b, 'B', [{'a': 'b_1'}], label='do-a-thing')

    env['runbot_merge.project']._check_progress()

    # add second commit blocking FF
    cn = repo_b.make_commit('heads/master', 'second', None, tree={'a': 'b_0', 'b': 'other'})

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

def test_one_failed(env, project, repo_a, repo_b):
    """ If the companion of a ready branch-matched PR is not ready,
    they should not get staged
    """
    project.batch_limit = 1
    c_a = repo_a.make_commit(None, 'initial', None, tree={'a': 'a_0'})
    repo_a.make_ref('heads/master', c_a)
    # pr_a is born ready
    pr_a = make_pr(repo_a, 'A', [{'a': 'a_1'}], label='do-a-thing')

    c_b = repo_b.make_commit(None, 'initial', None, tree={'a': 'b_0'})
    repo_b.make_ref('heads/master', c_b)
    c_pr = repo_b.make_commit(c_b, 'pr', None, tree={'a': 'b_1'})
    pr_b = repo_b.make_pr(
        'title', 'body', target='master', ctid=c_pr,
        user='user', label='user:do-a-thing',
    )
    repo_b.post_status(c_pr, 'success', 'ci/runbot')
    repo_b.post_status(c_pr, 'success', 'legal/cla')

    pr_a = to_pr(env, pr_a)
    pr_b = to_pr(env, pr_b)
    assert pr_a.state == 'ready'
    assert pr_b.state == 'validated'
    assert pr_a.label == pr_b.label == 'user:do-a-thing'

    env['runbot_merge.project']._check_progress()

    assert not pr_b.staging_id
    assert not pr_a.staging_id, \
        "pr_a should not have been staged as companion is not ready"

def test_batching(env, project, repo_a, repo_b):
    """ If multiple batches (label groups) are ready they should get batched
    together (within the limits of teh project's batch limit)
    """
    project.batch_limit = 3
    repo_a.make_ref('heads/master', repo_a.make_commit(None, 'initial', None, tree={'a': 'a0'}))
    repo_b.make_ref('heads/master', repo_b.make_commit(None, 'initial', None, tree={'b': 'b0'}))

    prs = [(
        a and to_pr(env, make_pr(repo_a, f'A{i}', [{f'a{i}': f'a{i}'}], label=f'batch{i}')),
        b and to_pr(env, make_pr(repo_b, f'B{i}', [{f'b{i}': f'b{i}'}], label=f'batch{i}'))
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

def test_batching_split(env, repo_a, repo_b):
    """ If a staging fails, it should get split properly across repos
    """
    repo_a.make_ref('heads/master', repo_a.make_commit(None, 'initial', None, tree={'a': 'a0'}))
    repo_b.make_ref('heads/master', repo_b.make_commit(None, 'initial', None, tree={'b': 'b0'}))

    prs = [(
        a and to_pr(env, make_pr(repo_a, f'A{i}', [{f'a{i}': f'a{i}'}], label=f'batch{i}')),
        b and to_pr(env, make_pr(repo_b, f'B{i}', [{f'b{i}': f'b{i}'}], label=f'batch{i}'))
    )
        for i, (a, b) in enumerate([(1, 1), (0, 1), (1, 1), (1, 1), (1, 0)])
    ]

    env['runbot_merge.project']._check_progress()

    st0 = env['runbot_merge.stagings'].search([])
    assert len(st0.batch_ids) == 5
    assert len(st0.mapped('batch_ids.prs')) == 8

    # mark b.staging as failed -> should create two new stagings with (0, 1)
    # and (2, 3, 4) and stage the first one
    repo_b.post_status('heads/staging.master', 'success', 'legal/cla')
    repo_b.post_status('heads/staging.master', 'failure', 'ci/runbot')

    env['runbot_merge.project']._check_progress()

    assert not st0.exists()
    sts = env['runbot_merge.stagings'].search([])
    assert len(sts) == 2
    st1, st2 = sts
    # a bit oddly st1 is probably the (2,3,4) one: the split staging for
    # (0, 1) has been "exploded" and a new staging was created for it
    assert not st1.heads
    assert len(st1.batch_ids) == 3
    assert st1.mapped('batch_ids.prs') == \
        prs[2][0] | prs[2][1] | prs[3][0] | prs[3][1] | prs[4][0]

    assert st2.heads
    assert len(st2.batch_ids) == 2
    assert st2.mapped('batch_ids.prs') == \
        prs[0][0] | prs[0][1] | prs[1][1]
