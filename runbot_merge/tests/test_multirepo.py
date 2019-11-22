""" The mergebot does not work on a dependency basis, rather all
repositories of a project are co-equal and get  (on target and
source branches).

When preparing a staging, we simply want to ensure branch-matched PRs
are staged concurrently in all repos
"""
import json

import pytest

from test_utils import re_matches, get_partner

@pytest.fixture
def repo_a(project, make_repo):
    repo = make_repo('a')
    project.write({'repo_ids': [(0, 0, {'name': repo.name})]})
    return repo

@pytest.fixture
def repo_b(project, make_repo):
    repo = make_repo('b')
    project.write({'repo_ids': [(0, 0, {'name': repo.name})]})
    return repo

@pytest.fixture
def repo_c(project, make_repo):
    repo = make_repo('c')
    project.write({'repo_ids': [(0, 0, {'name': repo.name})]})
    return repo

def make_pr(repo, prefix, trees, *, target='master', user,
            statuses=(('ci/runbot', 'success'), ('legal/cla', 'success')),
            reviewer):
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
    *_, c = repo.make_commits(
        'heads/{}'.format(target),
        *(
            repo.Commit('commit_{}_{:02}'.format(prefix, i), tree=tree)
            for i, tree in enumerate(trees)
        ),
        ref='heads/{}'.format(prefix)
    )
    pr = repo.make_pr(title='title {}'.format(prefix), body='body {}'.format(prefix),
                      target=target, head=prefix, token=user)
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

def test_stage_one(env, project, repo_a, repo_b, config):
    """ First PR is non-matched from A => should not select PR from B
    """
    project.batch_limit = 1

    with repo_a:
        make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
        pr_a = make_pr(
            repo_a, 'A', [{'a': 'a_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'])

    with repo_b:
        make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})
        pr_b = make_pr(
            repo_b, 'B', [{'a': 'b_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    env.run_crons()

    assert to_pr(env, pr_a).state == 'ready'
    assert to_pr(env, pr_a).staging_id
    assert to_pr(env, pr_b).state == 'ready'
    assert not to_pr(env, pr_b).staging_id

def test_stage_match(env, project, repo_a, repo_b, config):
    """ First PR is matched from A,  => should select matched PR from B
    """
    project.batch_limit = 1

    with repo_a:
        make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
        pr_a = make_pr(
            repo_a, 'do-a-thing', [{'a': 'a_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    with repo_b:
        make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})
        pr_b = make_pr(repo_b, 'do-a-thing', [{'a': 'b_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    env.run_crons()

    pr_a = to_pr(env, pr_a)
    pr_b = to_pr(env, pr_b)
    assert pr_a.state == 'ready'
    assert pr_a.staging_id
    assert pr_b.state == 'ready'
    assert pr_b.staging_id
    # should be part of the same staging
    assert pr_a.staging_id == pr_b.staging_id, \
        "branch-matched PRs should be part of the same staging"

    for repo in [repo_a, repo_b]:
        with repo:
            repo.post_status('staging.master', 'success', 'legal/cla')
            repo.post_status('staging.master', 'success', 'ci/runbot')
    env.run_crons()
    assert pr_a.state == 'merged'
    assert pr_b.state == 'merged'

    assert 'Related: {}#{}'.format(repo_b.name, pr_b.number) in repo_a.commit('master').message
    assert 'Related: {}#{}'.format(repo_a.name, pr_a.number) in repo_b.commit('master').message

def test_unmatch_patch(env, project, repo_a, repo_b, config):
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
    with repo_a:
        make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
        pr_a = make_pr(
            repo_a, 'patch-1', [{'a': 'a_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    with repo_b:
        make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})
        pr_b = make_pr(
            repo_b, 'patch-1', [{'a': 'b_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    env.run_crons()

    pr_a = to_pr(env, pr_a)
    pr_b = to_pr(env, pr_b)
    assert pr_a.state == 'ready'
    assert pr_a.staging_id
    assert pr_b.state == 'ready'
    assert not pr_b.staging_id, 'patch-* PRs should not be branch-matched'

def test_sub_match(env, project, repo_a, repo_b, repo_c, config):
    """ Branch-matching should work on a subset of repositories
    """
    project.batch_limit = 1
    with repo_a: # no pr here
        make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
    with repo_b:
        make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})
        pr_b = make_pr(
            repo_b, 'do-a-thing', [{'a': 'b_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    with repo_c:
        make_branch(repo_c, 'master', 'initial', {'a': 'c_0'})
        pr_c = make_pr(
            repo_c, 'do-a-thing', [{'a': 'c_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    env.run_crons()

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

def test_merge_fail(env, project, repo_a, repo_b, users, config):
    """ In a matched-branch scenario, if merging in one of the linked repos
    fails it should revert the corresponding merges
    """
    project.batch_limit = 1

    with repo_a, repo_b:
        make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
        make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})

        # first set of matched PRs
        pr1a = make_pr(
            repo_a, 'do-a-thing', [{'a': 'a_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
        pr1b = make_pr(
            repo_b, 'do-a-thing', [{'a': 'b_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )

        # add a conflicting commit to B so the staging fails
        repo_b.make_commit('heads/master', 'cn', None, tree={'a': 'cn'})

        # and a second set of PRs which should get staged while the first set
        # fails
        pr2a = make_pr(
            repo_a, 'do-b-thing', [{'b': 'ok'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
        pr2b = make_pr(
            repo_b, 'do-b-thing', [{'b': 'ok'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    env.run_crons()

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
    reviewer = get_partner(env, users["reviewer"]).formatted_email
    assert not other.staging_id
    assert [
        c['commit']['message']
        for c in repo_a.log('heads/staging.master')
    ] == [
        re_matches('^force rebuild'),
        """commit_do-b-thing_00

closes %s#%d

Related: %s#%d
Signed-off-by: %s""" % (repo_a.name, pr2a.number, repo_b.name, pr2b.number, reviewer),
        'initial'
    ], "dummy commit + squash-merged PR commit + root commit"

def test_ff_fail(env, project, repo_a, repo_b, config):
    """ In a matched-branch scenario, fast-forwarding one of the repos fails
    the entire thing should be rolled back
    """
    project.batch_limit = 1

    with repo_a, repo_b:
        root_a = make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
        make_pr(
            repo_a, 'do-a-thing', [{'a': 'a_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )

        make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})
        make_pr(
            repo_b, 'do-a-thing', [{'a': 'b_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    env.run_crons()

    # add second commit blocking FF
    with repo_b:
        cn = repo_b.make_commit('heads/master', 'second', None, tree={'a': 'b_0', 'b': 'other'})
    assert repo_b.commit('heads/master').id == cn

    with repo_a, repo_b:
        repo_a.post_status('heads/staging.master', 'success', 'ci/runbot')
        repo_a.post_status('heads/staging.master', 'success', 'legal/cla')
        repo_b.post_status('heads/staging.master', 'success', 'ci/runbot')
        repo_b.post_status('heads/staging.master', 'success', 'legal/cla')
    env.run_crons('runbot_merge.merge_cron')
    assert repo_b.commit('heads/master').id == cn,\
        "B should still be at the conflicting commit"
    assert repo_a.commit('heads/master').id == root_a,\
        "FF A should have been rolled back when B failed"

    # should be re-staged
    st = env['runbot_merge.stagings'].search([])
    assert len(st) == 1
    assert len(st.batch_ids.prs) == 2

class TestCompanionsNotReady:
    def test_one_pair(self, env, project, repo_a, repo_b, config, users):
        """ If the companion of a ready branch-matched PR is not ready,
        they should not get staged
        """
        project.batch_limit = 1
        with repo_a, repo_b:
            make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
            # pr_a is born ready
            p_a = make_pr(
                repo_a, 'do-a-thing', [{'a': 'a_1'}],
                user=config['role_user']['token'],
                reviewer=config['role_reviewer']['token'],
            )

            make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})
            p_b = make_pr(
                repo_b, 'do-a-thing', [{'a': 'b_1'}],
                user=config['role_user']['token'],
                reviewer=None,
            )

        pr_a = to_pr(env, p_a)
        pr_b = to_pr(env, p_b)
        assert pr_a.label == pr_b.label == '{}:do-a-thing'.format(config['github']['owner'])

        env.run_crons()

        assert pr_a.state == 'ready'
        assert pr_b.state == 'validated'
        assert not pr_b.staging_id
        assert not pr_a.staging_id, \
            "pr_a should not have been staged as companion is not ready"

        assert p_a.comments == [
            (users['reviewer'], 'hansen r+'),
            (users['user'], "Linked pull request(s) %s#%d not ready. Linked PRs are not staged until all of them are ready." % (repo_b.name, p_b.number)),
        ]
        # ensure the message is only sent once per PR
        env.run_crons('runbot_merge.check_linked_prs_status')
        assert p_a.comments == [
            (users['reviewer'], 'hansen r+'),
            (users['user'], "Linked pull request(s) %s#%d not ready. Linked PRs are not staged until all of them are ready." % (repo_b.name, p_b.number)),
        ]
        assert p_b.comments == []

    def test_two_of_three_unready(self, env, project, repo_a, repo_b, repo_c, users, config):
        """ In a 3-batch, if two of the PRs are not ready both should be
        linked by the first one
        """
        project.batch_limit = 1
        with repo_a, repo_b, repo_c:
            make_branch(repo_a, 'master', 'initial', {'f': 'a0'})
            pr_a = make_pr(
                repo_a, 'a-thing', [{'f': 'a1'}],
                user=config['role_user']['token'],
                reviewer=None,
            )

            make_branch(repo_b, 'master', 'initial', {'f': 'b0'})
            pr_b = make_pr(
                repo_b, 'a-thing', [{'f': 'b1'}],
                user=config['role_user']['token'],
                reviewer=config['role_reviewer']['token'],
            )

            make_branch(repo_c, 'master', 'initial', {'f': 'c0'})
            pr_c = make_pr(
                repo_c, 'a-thing', [{'f': 'c1'}],
                user=config['role_user']['token'],
                reviewer=None,
            )
        env.run_crons()

        assert pr_a.comments == []
        assert pr_b.comments == [
            (users['reviewer'], 'hansen r+'),
            (users['user'], "Linked pull request(s) %s#%d, %s#%d not ready. Linked PRs are not staged until all of them are ready." % (
                repo_a.name, pr_a.number,
                repo_c.name, pr_c.number
            ))
        ]
        assert pr_c.comments == []

    def test_one_of_three_unready(self, env, project, repo_a, repo_b, repo_c, users, config):
        """ In a 3-batch, if one PR is not ready it should be linked on the
        other two
        """
        project.batch_limit = 1
        with repo_a, repo_b, repo_c:
            make_branch(repo_a, 'master', 'initial', {'f': 'a0'})
            pr_a = make_pr(
                repo_a, 'a-thing', [{'f': 'a1'}],
                user=config['role_user']['token'],
                reviewer=None,
            )

            make_branch(repo_b, 'master', 'initial', {'f': 'b0'})
            pr_b = make_pr(
                repo_b, 'a-thing', [{'f': 'b1'}],
                user=config['role_user']['token'],
                reviewer=config['role_reviewer']['token'],
            )

            make_branch(repo_c, 'master', 'initial', {'f': 'c0'})
            pr_c = make_pr(
                repo_c, 'a-thing', [{'f': 'c1'}],
                user=config['role_user']['token'],
                reviewer=config['role_reviewer']['token'],
            )
        env.run_crons()

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

def test_other_failed(env, project, repo_a, repo_b, users, config):
    """ In a non-matched-branch scenario, if the companion staging (copy of
    targets) fails when built with the PR, it should provide a non-useless
    message
    """
    with repo_a, repo_b:
        make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
        # pr_a is born ready
        pr_a = make_pr(
            repo_a, 'do-a-thing', [{'a': 'a_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )

        make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})
    env.run_crons()

    pr = to_pr(env, pr_a)
    assert pr.staging_id

    with repo_a, repo_b:
        repo_a.post_status('heads/staging.master', 'success', 'legal/cla')
        repo_a.post_status('heads/staging.master', 'success', 'ci/runbot', target_url="http://example.org/a")
        repo_b.post_status('heads/staging.master', 'success', 'legal/cla')
        repo_b.post_status('heads/staging.master', 'failure', 'ci/runbot', target_url="http://example.org/b")
    env.run_crons()

    sth = repo_b.commit('heads/staging.master').id
    assert not pr.staging_id
    assert pr.state == 'error'
    assert pr_a.comments == [
        (users['reviewer'], 'hansen r+'),
        (users['user'], 'Staging failed: ci/runbot on %s (view more at http://example.org/b)' % sth)
    ]

class TestMultiBatches:
    def test_batching(self, env, project, repo_a, repo_b, config):
        """ If multiple batches (label groups) are ready they should get batched
        together (within the limits of teh project's batch limit)
        """
        project.batch_limit = 3

        with repo_a, repo_b:
            make_branch(repo_a, 'master', 'initial', {'a': 'a0'})
            make_branch(repo_b, 'master', 'initial', {'b': 'b0'})

            prs = [(
                a and to_pr(env, make_pr(repo_a, 'batch{}'.format(i), [{'a{}'.format(i): 'a{}'.format(i)}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'],)),
                b and to_pr(env, make_pr(repo_b, 'batch{}'.format(i), [{'b{}'.format(i): 'b{}'.format(i)}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'],))
            )
                for i, (a, b) in enumerate([(1, 1), (0, 1), (1, 1), (1, 1), (1, 0)])
            ]
        env.run_crons()

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

    def test_batching_split(self, env, repo_a, repo_b, config):
        """ If a staging fails, it should get split properly across repos
        """
        with repo_a, repo_b:
            make_branch(repo_a, 'master', 'initial', {'a': 'a0'})
            make_branch(repo_b, 'master', 'initial', {'b': 'b0'})

            prs = [(
                a and to_pr(env, make_pr(repo_a, 'batch{}'.format(i), [{'a{}'.format(i): 'a{}'.format(i)}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'],)),
                b and to_pr(env, make_pr(repo_b, 'batch{}'.format(i), [{'b{}'.format(i): 'b{}'.format(i)}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'],))
            )
                for i, (a, b) in enumerate([(1, 1), (0, 1), (1, 1), (1, 1), (1, 0)])
            ]
        env.run_crons()

        st0 = env['runbot_merge.stagings'].search([])
        assert len(st0.batch_ids) == 5
        assert len(st0.mapped('batch_ids.prs')) == 8

        # mark b.staging as failed -> should create two splits with (0, 1)
        # and (2, 3, 4) and stage the first one
        with repo_b:
            repo_b.post_status('heads/staging.master', 'success', 'legal/cla')
            repo_b.post_status('heads/staging.master', 'failure', 'ci/runbot')
        env.run_crons()

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

def test_urgent(env, repo_a, repo_b, config):
    """ Either PR of a co-dependent pair being p=0 leads to the entire pair
    being prioritized
    """
    with repo_a, repo_b:
        make_branch(repo_a, 'master', 'initial', {'a0': 'a'})
        make_branch(repo_b, 'master', 'initial', {'b0': 'b'})

        pr_a = make_pr(repo_a, 'batch', [{'a1': 'a'}, {'a2': 'a'}], user=config['role_user']['token'], reviewer=None, statuses=[])
        pr_b = make_pr(repo_b, 'batch', [{'b1': 'b'}, {'b2': 'b'}], user=config['role_user']['token'], reviewer=None, statuses=[])
        pr_c = make_pr(repo_a, 'C', [{'c1': 'c', 'c2': 'c'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'],)

        pr_a.post_comment('hansen rebase-merge', config['role_reviewer']['token'])
        pr_b.post_comment('hansen rebase-merge p=0', config['role_reviewer']['token'])
    env.run_crons()
    # should have batched pr_a and pr_b despite neither being reviewed or
    # approved
    p_a, p_b = to_pr(env, pr_a), to_pr(env, pr_b)
    p_c = to_pr(env, pr_c)
    assert p_a.batch_id and p_b.batch_id and p_a.batch_id == p_b.batch_id,\
        "a and b should have been recognised as co-dependent"
    assert not p_c.staging_id

class TestBlocked:
    def test_merge_method(self, env, repo_a, config):
        with repo_a:
            make_branch(repo_a, 'master', 'initial', {'a0': 'a'})

            pr = make_pr(repo_a, 'A', [{'a1': 'a'}, {'a2': 'a'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'],)
        env.run_crons()

        p = to_pr(env, pr)
        assert p.state == 'ready'
        assert p.blocked

        with repo_a: pr.post_comment('hansen rebase-merge', config['role_reviewer']['token'])
        assert not p.blocked

    def test_linked_closed(self, env, repo_a, repo_b, config):
        with repo_a, repo_b:
            make_branch(repo_a, 'master', 'initial', {'a0': 'a'})
            make_branch(repo_b, 'master', 'initial', {'b0': 'b'})

            pr = make_pr(repo_a, 'xxx', [{'a1': 'a'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'],)
            b = make_pr(repo_b, 'xxx', [{'b1': 'b'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'], statuses=[])
        env.run_crons()

        p = to_pr(env, pr)
        assert p.blocked
        with repo_b: b.close()
        # FIXME: find a way for PR.blocked to depend on linked PR somehow so this isn't needed
        p.invalidate_cache(['blocked'], [p.id])
        assert not p.blocked

    def test_linked_merged(self, env, repo_a, repo_b, config):
        with repo_a, repo_b:
            make_branch(repo_a, 'master', 'initial', {'a0': 'a'})
            make_branch(repo_b, 'master', 'initial', {'b0': 'b'})

            b = make_pr(repo_b, 'xxx', [{'b1': 'b'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'],)
        env.run_crons() # stage b and c

        with repo_a, repo_b:
            repo_a.post_status('heads/staging.master', 'success', 'legal/cla')
            repo_a.post_status('heads/staging.master', 'success', 'ci/runbot')
            repo_b.post_status('heads/staging.master', 'success', 'legal/cla')
            repo_b.post_status('heads/staging.master', 'success', 'ci/runbot')
        env.run_crons() # merge b and c
        assert to_pr(env, b).state == 'merged'

        with repo_a:
            pr = make_pr(repo_a, 'xxx', [{'a1': 'a'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'],)
        env.run_crons() # merge b and c

        p = to_pr(env, pr)
        assert not p.blocked

    def test_linked_unready(self, env, repo_a, repo_b, config):
        """ Create a PR A linked to a non-ready PR B,
        * A is blocked by default
        * A is not blocked if A.p=0
        * A is not blocked if B.p=0
        """
        with repo_a, repo_b:
            make_branch(repo_a, 'master', 'initial', {'a0': 'a'})
            make_branch(repo_b, 'master', 'initial', {'b0': 'b'})

            a = make_pr(repo_a, 'xxx', [{'a1': 'a'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'],)
            b = make_pr(repo_b, 'xxx', [{'b1': 'b'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'], statuses=[])
        env.run_crons()

        pr_a = to_pr(env, a)
        assert pr_a.blocked

        with repo_a: a.post_comment('hansen p=0', config['role_reviewer']['token'])
        assert not pr_a.blocked

        with repo_a: a.post_comment('hansen p=2', config['role_reviewer']['token'])
        assert pr_a.blocked

        with repo_b: b.post_comment('hansen p=0', config['role_reviewer']['token'])
        assert not pr_a.blocked
