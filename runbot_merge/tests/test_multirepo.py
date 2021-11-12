""" The mergebot does not work on a dependency basis, rather all
repositories of a project are co-equal and get  (on target and
source branches).

When preparing a staging, we simply want to ensure branch-matched PRs
are staged concurrently in all repos
"""
import json
import time

import pytest
import requests
from lxml.etree import XPath, tostring

from utils import seen, re_matches, get_partner, pr_page, to_pr, Commit


@pytest.fixture
def repo_a(project, make_repo, setreviewers):
    repo = make_repo('a')
    r = project.env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'required_statuses': 'legal/cla,ci/runbot',
        'group_id': False,
    })
    setreviewers(r)
    return repo

@pytest.fixture
def repo_b(project, make_repo, setreviewers):
    repo = make_repo('b')
    r = project.env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'required_statuses': 'legal/cla,ci/runbot',
        'group_id': False,
    })
    setreviewers(r)
    return repo

@pytest.fixture
def repo_c(project, make_repo, setreviewers):
    repo = make_repo('c')
    r = project.env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'required_statuses': 'legal/cla,ci/runbot',
        'group_id': False,
    })
    setreviewers(r)
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

    pra_id = to_pr(env, pr_a)
    assert pra_id.state == 'ready'
    assert pra_id.staging_id
    assert repo_a.commit('staging.master').message.startswith('commit_A_00')
    assert repo_b.commit('staging.master').message.startswith('force rebuild')

    prb_id = to_pr(env, pr_b)
    assert prb_id.state == 'ready'
    assert not prb_id.staging_id

get_related_pr_labels = XPath('.//*[normalize-space(text()) = "Linked pull requests"]//a/text()')
def test_stage_match(env, project, repo_a, repo_b, config, page):
    """ First PR is matched from A,  => should select matched PR from B
    """
    project.batch_limit = 1

    with repo_a:
        make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
        prx_a = make_pr(
            repo_a, 'do-a-thing', [{'a': 'a_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    with repo_b:
        make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})
        prx_b = make_pr(repo_b, 'do-a-thing', [{'a': 'b_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    pr_a = to_pr(env, prx_a)
    pr_b = to_pr(env, prx_b)

    # check that related PRs link to one another
    assert get_related_pr_labels(pr_page(page, prx_a)) == pr_b.mapped('display_name')
    assert get_related_pr_labels(pr_page(page, prx_b)) == pr_a.mapped('display_name')

    env.run_crons()

    assert pr_a.state == 'ready'
    assert pr_a.staging_id
    assert pr_b.state == 'ready'
    assert pr_b.staging_id
    # should be part of the same staging
    assert pr_a.staging_id == pr_b.staging_id, \
        "branch-matched PRs should be part of the same staging"

    # check that related PRs *still* link to one another during staging
    assert get_related_pr_labels(pr_page(page, prx_a)) == [pr_b.display_name]
    assert get_related_pr_labels(pr_page(page, prx_b)) == [pr_a.display_name]
    with repo_a:
        repo_a.post_status('staging.master', 'failure', 'legal/cla')
    env.run_crons()

    assert pr_a.state == 'error'
    assert pr_b.state == 'ready'

    with repo_a:
        prx_a.post_comment('hansen retry', config['role_reviewer']['token'])
    env.run_crons()

    assert pr_a.state == pr_b.state == 'ready'
    assert pr_a.staging_id and pr_b.staging_id
    for repo in [repo_a, repo_b]:
        with repo:
            repo.post_status('staging.master', 'success', 'legal/cla')
            repo.post_status('staging.master', 'success', 'ci/runbot')
    env.run_crons()
    assert pr_a.state == 'merged'
    assert pr_b.state == 'merged'

    assert 'Related: {}'.format(pr_b.display_name) in repo_a.commit('master').message
    assert 'Related: {}'.format(pr_a.display_name) in repo_b.commit('master').message

    print(pr_a.batch_ids.read(['staging_id', 'prs']))
    # check that related PRs *still* link to one another after merge
    assert get_related_pr_labels(pr_page(page, prx_a)) == [pr_b.display_name]
    assert get_related_pr_labels(pr_page(page, prx_b)) == [pr_a.display_name]

def test_different_targets(env, project, repo_a, repo_b, config):
    """ PRs with different targets should not be matched together
    """
    project.write({
        'batch_limit': 1,
        'branch_ids': [(0, 0, {'name': 'other'})]
    })
    with repo_a:
        make_branch(repo_a, 'master', 'initial', {'master': 'a_0'})
        make_branch(repo_a, 'other', 'initial', {'other': 'a_0'})
        pr_a = make_pr(
            repo_a, 'do-a-thing', [{'mater': 'a_1'}],
            target='master',
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    with repo_b:
        make_branch(repo_b, 'master', 'initial', {'master': 'b_0'})
        make_branch(repo_b, 'other', 'initial', {'other': 'b_0'})
        pr_b = make_pr(
            repo_b, 'do-a-thing', [{'other': 'b_1'}],
            target='other',
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
            statuses=[],
        )
    time.sleep(5)
    env.run_crons()

    pr_a = to_pr(env, pr_a)
    pr_b = to_pr(env, pr_b)
    assert pr_a.state == 'ready'
    assert not pr_a.blocked
    assert pr_a.staging_id

    assert pr_b.blocked
    assert pr_b.state == 'approved'
    assert not pr_b.staging_id

    for r in [repo_a, repo_b]:
        with r:
            r.post_status('staging.master', 'success', 'legal/cla')
            r.post_status('staging.master', 'success', 'ci/runbot')
    env.run_crons()
    assert pr_a.state == 'merged'

def test_stage_different_statuses(env, project, repo_a, repo_b, config):
    project.batch_limit = 1

    env['runbot_merge.repository'].search([
        ('name', '=', repo_b.name)
    ]).write({
        'required_statuses': 'foo/bar',
    })

    with repo_a:
        make_branch(repo_a, 'master', 'initial', {'a': 'a_0'})
        pr_a = make_pr(
            repo_a, 'do-a-thing', [{'a': 'a_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
        repo_a.post_status(pr_a.head, 'success', 'foo/bar')
    with repo_b:
        make_branch(repo_b, 'master', 'initial', {'a': 'b_0'})
        [c] = repo_b.make_commits(
            'heads/master',
            repo_b.Commit('some_commit\n\nSee also %s#%d' % (repo_a.name, pr_a.number), tree={'a': 'b_1'}),
            ref='heads/do-a-thing'
        )
        pr_b = repo_b.make_pr(
            title="title", body="body", target='master', head='do-a-thing',
            token=config['role_user']['token'])
        repo_b.post_status(c, 'success', 'ci/runbot')
        repo_b.post_status(c, 'success', 'legal/cla')
        pr_b.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    # since the labels are the same but the statuses on pr_b are not the
    # expected ones, pr_a should be blocked on pr_b, which should be approved
    # but not validated / ready
    pr_a_id = to_pr(env, pr_a)
    pr_b_id = to_pr(env, pr_b)
    assert pr_a_id.state == 'ready'
    assert not pr_a_id.staging_id
    assert pr_a_id.blocked
    assert pr_b_id.state == 'approved'
    assert not pr_b_id.staging_id

    with repo_b:
        repo_b.post_status(pr_b.head, 'success', 'foo/bar')
    env.run_crons()

    assert pr_a_id.state == pr_b_id.state == 'ready'
    assert pr_a_id.staging_id == pr_b_id.staging_id

    # do the actual merge to check for the Related header
    for repo in [repo_a, repo_b]:
        with repo:
            repo.post_status('staging.master', 'success', 'legal/cla')
            repo.post_status('staging.master', 'success', 'ci/runbot')
            repo.post_status('staging.master', 'success', 'foo/bar')
    env.run_crons()

    pr_a_ref = to_pr(env, pr_a).display_name
    pr_b_ref = to_pr(env, pr_b).display_name
    master_a = repo_a.commit('master')
    master_b = repo_b.commit('master')

    assert 'Related: {}'.format(pr_b_ref) in master_a.message,\
        "related should be in PR A's message"
    assert 'Related: {}'.format(pr_a_ref) not in master_b.message,\
        "related should not be in PR B's message since the ref' was added explicitly"
    assert pr_a_ref in master_b.message, "the ref' should still be there though"

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
    a_staging = repo_a.commit('staging.master')
    b_staging = repo_b.commit('staging.master')
    c_staging = repo_c.commit('staging.master')
    assert json.loads(st.heads) == {
        repo_a.name: a_staging.id,
        repo_a.name + '^': a_staging.parents[0],
        repo_b.name: b_staging.id,
        repo_b.name + '^': b_staging.id,
        repo_c.name: c_staging.id,
        repo_c.name + '^': c_staging.id,
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
        seen(env, pr1b, users),
        (users['user'], re_matches('^Unable to stage PR')),
    ]
    other = to_pr(env, pr1a)
    reviewer = get_partner(env, users["reviewer"]).formatted_email
    assert not other.staging_id
    assert [
        c['commit']['message']
        for c in repo_a.log('heads/staging.master')
    ] == [
        """commit_do-b-thing_00

closes %s

Related: %s
Signed-off-by: %s""" % (s2[0].display_name, s2[1].display_name, reviewer),
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
    env.run_crons('runbot_merge.merge_cron', 'runbot_merge.staging_cron')
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
            seen(env, p_a, users),
            (users['user'], "Linked pull request(s) %s#%d not ready. Linked PRs are not staged until all of them are ready." % (repo_b.name, p_b.number)),
        ]
        # ensure the message is only sent once per PR
        env.run_crons('runbot_merge.check_linked_prs_status')
        assert p_a.comments == [
            (users['reviewer'], 'hansen r+'),
            seen(env, p_a, users),
            (users['user'], "Linked pull request(s) %s#%d not ready. Linked PRs are not staged until all of them are ready." % (repo_b.name, p_b.number)),
        ]
        assert p_b.comments == [seen(env, p_b, users)]

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

        assert pr_a.comments == [seen(env, pr_a, users)]
        assert pr_b.comments == [
            (users['reviewer'], 'hansen r+'),
            seen(env, pr_b, users),
            (users['user'], "Linked pull request(s) %s#%d, %s#%d not ready. Linked PRs are not staged until all of them are ready." % (
                repo_a.name, pr_a.number,
                repo_c.name, pr_c.number
            ))
        ]
        assert pr_c.comments == [seen(env, pr_c, users)]

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

        assert pr_a.comments == [seen(env, pr_a, users)]
        assert pr_b.comments == [
            (users['reviewer'], 'hansen r+'),
            seen(env, pr_b, users),
            (users['user'], "Linked pull request(s) %s#%d not ready. Linked PRs are not staged until all of them are ready." % (
                repo_a.name, pr_a.number
            ))
        ]
        assert pr_c.comments == [
            (users['reviewer'], 'hansen r+'),
            seen(env, pr_c, users),
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
        seen(env, pr_a, users),
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

def test_different_branches(env, project, repo_a, repo_b, config):
    project.write({
        'branch_ids': [(0, 0, {'name': 'dev'})]
    })
    # repo_b only works with master
    env['runbot_merge.repository'].search([('name', '=', repo_b.name)])\
        .branch_filter = '[("name", "=", "master")]'
    with repo_a, repo_b:
        make_branch(repo_a, 'dev', 'initial', {'a': '0'})
        make_branch(repo_a, 'master', 'initial', {'b': '0'})
        make_branch(repo_b, 'master', 'initial', {'b': '0'})

        pr_a = make_pr(
            repo_a, 'xxx', [{'a': '1'}],
            target='dev',
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token']
        )
    env.run_crons()

    with repo_a:
        pr_a.post_comment('hansen r+', config['role_reviewer']['token'])
        repo_a.post_status('heads/staging.dev', 'success', 'legal/cla')
        repo_a.post_status('heads/staging.dev', 'success', 'ci/runbot')
    env.run_crons()

    assert to_pr(env, pr_a).state == 'merged'

def test_remove_acl(env, partners, repo_a, repo_b, repo_c):
    """ Check that our way of deprovisioning works correctly
    """
    r = partners['self_reviewer']
    assert r.mapped('review_rights.repository_id.name') == [repo_a.name, repo_b.name, repo_c.name]
    r.write({'review_rights': [(5, 0, 0)]})
    assert r.mapped('review_rights.repository_id') == env['runbot_merge.repository']

class TestSubstitutions:
    def test_substitution_patterns(self, env, port):
        p = env['runbot_merge.project'].create({
            'name': 'proj',
            'github_token': 'wheeee',
            'repo_ids': [(0, 0, {'name': 'xxx/xxx'})],
            'branch_ids': [(0, 0, {'name': 'master'})]
        })
        r = p.repo_ids
        # replacement pattern, pr label, stored label
        cases = [
            ('/^foo:/foo-dev:/', 'foo:bar', 'foo-dev:bar'),
            ('/^foo:/foo-dev:/', 'foox:bar', 'foox:bar'),
            ('/^foo:/foo-dev:/i', 'FOO:bar', 'foo-dev:bar'),
            ('/o/x/g', 'foo:bar', 'fxx:bar'),
            ('@foo:@bar:@', 'foo:bar', 'bar:bar'),
            ('/foo:/bar:/\n/bar:/baz:/', 'foo:bar', 'baz:bar'),
        ]
        for pr_number, (pattern, original, target) in enumerate(cases, start=1):
            r.substitutions = pattern
            requests.post(
                'http://localhost:{}/runbot_merge/hooks'.format(port),
                headers={'X-Github-Event': 'pull_request'},
                json={
                    'action': 'opened',
                    'repository': {
                        'full_name': r.name,
                    },
                    'pull_request': {
                        'state': 'open',
                        'draft': False,
                        'user': {'login': 'bob'},
                        'base': {
                            'repo': {'full_name': r.name},
                            'ref': p.branch_ids.name,
                        },
                        'number': pr_number,
                        'title': "a pr",
                        'body': None,
                        'commits': 1,
                        'head': {
                            'label': original,
                            'sha': format(pr_number, 'x')*40,
                        }
                    }
                }
            )
            pr = env['runbot_merge.pull_requests'].search([
                ('repository', '=', r.id),
                ('number', '=', pr_number)
            ])
            assert pr.label == target


    def test_substitutions_staging(self, env, repo_a, repo_b, config):
        """ Different repos from the same project may have different policies for
        sourcing PRs. So allow for remapping labels on input in order to match.
        """
        repo_b_id = env['runbot_merge.repository'].search([
            ('name', '=', repo_b.name)
        ])
        # in repo b, replace owner part by repo_a's owner
        repo_b_id.substitutions = r"/.+:/%s:/" % repo_a.owner

        with repo_a:
            make_branch(repo_a, 'master', 'initial', {'a': '0'})
        with repo_b:
            make_branch(repo_b, 'master', 'initial', {'b': '0'})

        # policy is that repo_a PRs are created in the same repo while repo_b PRs
        # are created in personal forks
        with repo_a:
            repo_a.make_commits('master', repo_a.Commit('bop', tree={'a': '1'}), ref='heads/abranch')
            pra = repo_a.make_pr(target='master', head='abranch')
        b_fork = repo_b.fork()
        with b_fork, repo_b:
            b_fork.make_commits('master', b_fork.Commit('pob', tree={'b': '1'}), ref='heads/abranch')
            prb = repo_b.make_pr(
                title="a pr",
                target='master', head='%s:abranch' % b_fork.owner
            )

        pra_id = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo_a.name),
            ('number', '=', pra.number)
        ])
        prb_id = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo_b.name),
            ('number', '=', prb.number)
        ])
        assert pra_id.label.endswith(':abranch')
        assert prb_id.label.endswith(':abranch')

        with repo_a, repo_b:
            repo_a.post_status(pra.head, 'success', 'legal/cla')
            repo_a.post_status(pra.head, 'success', 'ci/runbot')
            pra.post_comment('hansen r+', config['role_reviewer']['token'])

            repo_b.post_status(prb.head, 'success', 'legal/cla')
            repo_b.post_status(prb.head, 'success', 'ci/runbot')
            prb.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        assert pra_id.staging_id, 'PR A should be staged'
        assert prb_id.staging_id, "PR B should be staged"
        assert pra_id.staging_id == prb_id.staging_id, "both prs should be staged together"
        assert pra_id.batch_id == prb_id.batch_id, "both prs should be part of the same batch"

def test_multi_project(env, make_repo, setreviewers, users, config,
                       tunnel):
    """ There should be no linking of PRs across projects, even if there is some
    structural overlap between the two.

    Here we have two projects on different forks, then a user creates a PR from
    a third fork (or one of the forks should not matter) to *both*.

    The two PRs should be independent.
    """
    Projects = env['runbot_merge.project']
    gh_token = config['github']['token']

    r1 = make_repo("repo_a")
    with r1:
        r1.make_commits(
            None, Commit('root', tree={'a': 'a'}),
            ref='heads/default')
    r1_dev = r1.fork()
    p1 = Projects.create({
        'name': 'Project 1',
        'github_token': gh_token,
        'github_prefix': 'hansen',
        'repo_ids': [(0, 0, {
            'name': r1.name,
            'group_id': False,
            'required_statuses': 'a',
        })],
        'branch_ids': [(0, 0, {'name': 'default'})],
    })
    setreviewers(*p1.repo_ids)

    r2 = make_repo('repo_b')
    with r2:
        r2.make_commits(
            None, Commit('root', tree={'b': 'a'}),
            ref='heads/default'
        )
    r2_dev = r2.fork()
    p2 = Projects.create({
        'name': "Project 2",
        'github_token': gh_token,
        'github_prefix': 'hansen',
        'repo_ids': [(0, 0, {
            'name': r2.name,
            'group_id': False,
            'required_statuses': 'a',
        })],
        'branch_ids': [(0, 0, {'name': 'default'})],
    })
    setreviewers(*p2.repo_ids)

    assert r1_dev.owner == r2_dev.owner

    with r1, r1_dev:
        r1_dev.make_commits('default', Commit('new', tree={'a': 'b'}), ref='heads/other')

        # create, validate, and approve pr1
        pr1 = r1.make_pr(title='pr 1', target='default', head=r1_dev.owner + ':other')
        r1.post_status(pr1.head, 'success', 'a')
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])

    with r2, r2_dev:
        r2_dev.make_commits('default', Commit('new', tree={'b': 'b'}), ref='heads/other')

        # create second PR with the same label *in a different project*, don't
        # approve it
        pr2 = r2.make_pr(title='pr 2', target='default', head=r2_dev.owner + ':other')
        r2.post_status(pr2.head, 'success', 'a')
    env.run_crons()

    pr1_id = to_pr(env, pr1)
    pr2_id = to_pr(env, pr2)

    print(
        pr1.repo.name, pr1.number, pr1_id.display_name, pr1_id.label,
        '\n',
        pr2.repo.name, pr2.number, pr2_id.display_name, pr2_id.label,
        flush=True,
    )

    assert pr1_id.state == 'ready' and not pr1_id.blocked
    assert pr2_id.state == 'validated'

    assert pr1_id.staging_id
    assert not pr2_id.staging_id

    assert pr1.comments == [
        (users['reviewer'], 'hansen r+'),
        (users['user'], f'[Pull request status dashboard]({pr1_id.url}).'),
    ]
    assert pr2.comments == [
        (users['user'], f'[Pull request status dashboard]({pr2_id.url}).'),
    ]

def test_freeze_complete(env, project, repo_a, repo_b, repo_c, users, config):
    """ Tests the freeze wizard feature (aside from the UI):

    * have a project with 3 repos, and two branches (1.0 and master) each
    * have 2 PRs required for the freeze
    * prep 3 freeze PRs
    * trigger the freeze wizard
    * trigger it again (check that the same object is returned, there should
      only be one freeze per project at a time)
    * configure the freeze
    * check that it doesn't go through
    * merge required PRs
    * check that freeze goes through
    * check that reminder is shown
    * check that new branches are created w/ correct parent & commit info
    """
    project.freeze_reminder = "Don't forget to like and subscribe"

    # have a project with 3 repos, and two branches (1.0 and master)
    project.branch_ids = [
        (1, project.branch_ids.id, {'sequence': 1}),
        (0, 0, {'name': '1.0', 'sequence': 2}),
    ]

    masters = []
    for r in [repo_a, repo_b, repo_c]:
        with r:
            [root, _] = r.make_commits(
                None,
                Commit('base', tree={'version': '', 'f': '0'}),
                Commit('release 1.0', tree={'version': '1.0'} if r is repo_a else None),
                ref='heads/1.0'
            )
            masters.extend(r.make_commits(root, Commit('other', tree={'f': '1'}), ref='heads/master'))

    # have 2 PRs required for the freeze
    with repo_a:
        repo_a.make_commits('master', Commit('super important file', tree={'g': 'x'}), ref='heads/apr')
        pr_required_a = repo_a.make_pr(target='master', head='apr')
    with repo_c:
        repo_c.make_commits('master', Commit('update thing', tree={'f': '2'}), ref='heads/cpr')
        pr_required_c = repo_c.make_pr(target='master', head='cpr')

    # have 3 release PRs, only the first one updates the tree (version file)
    with repo_a:
        repo_a.make_commits(
            masters[0],
            Commit('Release 1.1 (A)', tree={'version': '1.1'}),
            ref='heads/release-1.1'
        )
        pr_rel_a = repo_a.make_pr(target='master', head='release-1.1')
    with repo_b:
        repo_b.make_commits(
            masters[1],
            Commit('Release 1.1 (B)', tree=None),
            ref='heads/release-1.1'
        )
        pr_rel_b = repo_b.make_pr(target='master', head='release-1.1')
    with repo_c:
        repo_c.make_commits(
            masters[2],
            Commit('Release 1.1 (C)', tree=None),
            ref='heads/release-1.1'
        )
        pr_rel_c = repo_c.make_pr(target='master', head='release-1.1')
    env.run_crons() # process the PRs

    release_prs = {
        repo_a.name: to_pr(env, pr_rel_a),
        repo_b.name: to_pr(env, pr_rel_b),
        repo_c.name: to_pr(env, pr_rel_c),
    }

    # trigger the ~~tree~~ freeze wizard
    w = project.action_prepare_freeze()
    w2 = project.action_prepare_freeze()
    assert w == w2, "each project should only have one freeze wizard active at a time"

    w_id = env[w['res_model']].browse([w['res_id']])
    assert w_id.branch_name == '1.1', "check that the forking incremented the minor by 1"
    assert len(w_id.release_pr_ids) == len(project.repo_ids), \
        "should ask for a many release PRs as we have repositories"

    # configure required PRs
    w_id.required_pr_ids = (to_pr(env, pr_required_a) | to_pr(env, pr_required_c)).ids
    # configure releases
    for r in w_id.release_pr_ids:
        r.pr_id = release_prs[r.repository_id.name].id
    r = w_id.action_freeze()
    assert r == w, "the freeze is not ready so the wizard should redirect to itself"
    assert w_id.errors == "* 2 required PRs not ready."

    with repo_a:
        pr_required_a.post_comment('hansen r+', config['role_reviewer']['token'])
        repo_a.post_status('apr', 'success', 'ci/runbot')
        repo_a.post_status('apr', 'success', 'legal/cla')
    with repo_c:
        pr_required_c.post_comment('hansen r+', config['role_reviewer']['token'])
        repo_c.post_status('cpr', 'success', 'ci/runbot')
        repo_c.post_status('cpr', 'success', 'legal/cla')
    env.run_crons()

    for repo in [repo_a, repo_b, repo_c]:
        with repo:
            repo.post_status('staging.master', 'success', 'ci/runbot')
            repo.post_status('staging.master', 'success', 'legal/cla')
    env.run_crons()

    assert to_pr(env, pr_required_a).state == 'merged'
    assert to_pr(env, pr_required_c).state == 'merged'

    assert not w_id.errors

    r = w_id.action_freeze()
    # check that the wizard was deleted
    assert not w_id.exists()
    # check that the wizard pops out a reminder dialog (kinda)
    assert r['res_model'] == 'runbot_merge.project'
    assert r['res_id'] == project.id

    env.run_crons() # stage the release prs
    for repo in [repo_a, repo_b, repo_c]:
        with repo:
            repo.post_status('staging.1.1', 'success', 'ci/runbot')
            repo.post_status('staging.1.1', 'success', 'legal/cla')
    env.run_crons() # get the release prs merged
    for pr_id in release_prs.values():
        assert pr_id.target.name == '1.1'
        assert pr_id.state == 'merged'

    c_a = repo_a.commit('1.1')
    assert c_a.message.startswith('Release 1.1 (A)')
    assert repo_a.read_tree(c_a) == {
        'f': '1', # from master
        'g': 'x', # from required pr
        'version': '1.1', # from release commit
    }
    c_a_parent = repo_a.commit(c_a.parents[0])
    assert c_a_parent.message.startswith('super important file')
    assert c_a_parent.parents[0] == masters[0]

    c_b = repo_b.commit('1.1')
    assert c_b.message.startswith('Release 1.1 (B)')
    assert repo_b.read_tree(c_b) == {'f': '1', 'version': ''}
    assert c_b.parents[0] == masters[1]

    c_c = repo_c.commit('1.1')
    assert c_c.message.startswith('Release 1.1 (C)')
    assert repo_c.read_tree(c_c) == {'f': '2', 'version': ''}
    assert repo_c.commit(c_c.parents[0]).parents[0] == masters[2]
