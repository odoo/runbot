""" The mergebot does not work on a dependency basis, rather all
repositories of a project are co-equal and get  (on target and
source branches).

When preparing a staging, we simply want to ensure branch-matched PRs
are staged concurrently in all repos
"""
import functools
import operator
import time
import xmlrpc.client
from itertools import repeat

import pytest
import requests
from lxml.etree import XPath

from utils import seen, get_partner, pr_page, to_pr, Commit, read_tracking_value


@pytest.fixture
def repo_a(env, project, make_repo, setreviewers):
    repo = make_repo('a')
    r = env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'required_statuses': 'default',
        'group_id': False,
    })
    setreviewers(r)
    env['runbot_merge.events_sources'].create({'repository': r.name})
    return repo

@pytest.fixture
def repo_b(env, project, make_repo, setreviewers):
    repo = make_repo('b')
    r = env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'required_statuses': 'default',
        'group_id': False,
    })
    setreviewers(r)
    env['runbot_merge.events_sources'].create({'repository': r.name})
    return repo

@pytest.fixture
def repo_c(env, project, make_repo, setreviewers):
    repo = make_repo('c')
    r = env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'required_statuses': 'default',
        'group_id': False,
    })
    setreviewers(r)
    env['runbot_merge.events_sources'].create({'repository': r.name})
    return repo

def make_pr(repo, prefix, trees, *, target='master', user,
            statuses=(('default', 'success'),),
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


@pytest.mark.parametrize('uniquifier', [False, True])
def test_stage_one(env, project, repo_a, repo_b, config, uniquifier):
    """ First PR is non-matched from A => should not select PR from B
    """
    project.uniquifier = uniquifier
    project.batch_limit = 1

    with repo_a:
        repo_a.make_commits(None, Commit('initial', tree={'a': 'a_0'}), ref='heads/master')
        pr_a = make_pr(
            repo_a, 'A', [{'a': 'a_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'])

    with repo_b:
        repo_b.make_commits(None, Commit('initial', tree={'a': 'b_0'}), ref='heads/master')
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
    if uniquifier:
        assert repo_b.commit('staging.master').message.startswith('force rebuild')
    else:
        assert repo_b.commit('staging.master').message == 'initial'

    prb_id = to_pr(env, pr_b)
    assert prb_id.state == 'ready'
    assert not prb_id.staging_id

get_related_pr_labels = XPath('.//*[normalize-space(text()) = "Linked pull requests"]//a/text()')
def test_stage_match(env, project, repo_a, repo_b, config, page):
    """ First PR is matched from A,  => should select matched PR from B
    """
    project.batch_limit = 1

    with repo_a:
        repo_a.make_commits(None, Commit('initial', tree={'a': 'a_0'}), ref='heads/master')
        prx_a = make_pr(
            repo_a, 'do-a-thing', [{'a': 'a_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    with repo_b:
        repo_b.make_commits(None, Commit('initial', tree={'a': 'b_0'}), ref='heads/master')
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
        repo_a.post_status('staging.master', 'failure')
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
            repo.post_status('staging.master', 'success')
    env.run_crons()
    assert pr_a.state == 'merged'
    assert pr_b.state == 'merged'

    assert 'Related: {}'.format(pr_b.display_name) in repo_a.commit('master').message
    assert 'Related: {}'.format(pr_a.display_name) in repo_b.commit('master').message

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
        repo_a.make_commits(None, Commit('initial', tree={'master': 'a_0'}), ref='heads/master')
        repo_a.make_commits(None, Commit('initial', tree={'other': 'a_0'}), ref='heads/other')
        pr_a = make_pr(
            repo_a, 'do-a-thing', [{'mater': 'a_1'}],
            target='master',
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    with repo_b:
        repo_b.make_commits(None, Commit('initial', tree={'master': 'b_0'}), ref='heads/master')
        repo_b.make_commits(None, Commit('initial', tree={'other': 'b_0'}), ref='heads/other')
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
            r.post_status('staging.master', 'success')
    env.run_crons()
    assert pr_a.state == 'merged'

def test_rejoin_different_batches(env, project, repo_a, repo_b, config):
    """In the case where a user makes a mistake and creates PRs with the same
    label targeting different branches, we want a retarget to merge the batches
    so the PRs get rejoined.
    """
    project.write({'branch_ids': [(0, 0, {'name': 'other'})]})
    with repo_a, repo_b:
        repo_a.make_commits(None, Commit('initial', tree={'master': 'a_0'}), ref='heads/master')
        repo_b.make_commits(None, Commit('initial', tree={'master': 'b_0'}), ref='heads/master')
        repo_a.make_commits('master', Commit('initial', tree={'other': 'a_0'}), ref='heads/other')
        repo_b.make_commits('master', Commit('initial', tree={'other': 'b_0'}), ref='heads/other')
    with repo_a, repo_b:
        pr1 = make_pr(
            repo_a, "foo", [{'x': 'y'}],
            target='master',
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
        pr2 = make_pr(
            repo_b, "foo", [{'x': 'y'}],
            target="other",
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
        pr2.post_comment("hansen rebase-ff", config['role_reviewer']['token'])
    env.run_crons()

    pr1_id = to_pr(env, pr1)
    pr2_id = to_pr(env, pr2)
    # the PRs are in different batches, and because they target different
    # branches they should also be in different stagings
    assert pr1_id.batch_id != pr2_id.batch_id
    assert pr1_id.staging_id and pr2_id.staging_id
    assert pr1_id.staging_id != pr2_id.staging_id

    with repo_b:
        pr2.base = 'master'
    assert pr1_id.target == pr2_id.target
    assert pr1_id.batch_id == pr2_id.batch_id
    env.run_crons()
    assert pr1_id.staging_id and pr2_id.staging_id
    assert pr1_id.staging_id == pr2_id.staging_id

    with repo_a, repo_b:
        repo_a.post_status('staging.master', 'success')
        repo_b.post_status('staging.master', 'success')
    env.run_crons()

    assert repo_a.read_tree(repo_a.commit('master')) == {
        'master': 'a_0',
        'x': 'y',
    }
    assert repo_b.read_tree(repo_b.commit('master')) == {
        'master': 'b_0',
        'other': 'b_0',
        'x': 'y',
    }

def test_rejoin_no_duplicates(env, project, repo_a, repo_b, config):
    """An exclusionary subcase of the previous is if the candidate batch for
    merging already has a PR for the repository.

    This is pretty unlikely to happen (barring on the fly renames /
    reattachments) as the duplicates would need to be created from the same
    branch or something...
    """
    project.write({'branch_ids': [(0, 0, {'name': 'other'})]})
    with repo_a, repo_b:
        repo_a.make_commits(None, Commit('initial', tree={'master': 'a_0'}), ref='heads/master')
        repo_b.make_commits(None, Commit('initial', tree={'master': 'b_0'}), ref='heads/master')
        repo_a.make_commits(None, Commit('initial', tree={'other': 'a_0'}), ref='heads/other')
        repo_b.make_commits(None, Commit('initial', tree={'other': 'b_0'}), ref='heads/other')
    with repo_a:
        pr1_a = make_pr(
            repo_a, "foo", [{'x': 'y'}],
            target='master',
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    with repo_b:
        pr1_b = make_pr(
            repo_b, "foo", [{'x': 'y'}],
            target='master',
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
        # can't use `foo` as label / branch, because it fucks with pr1_b
        pr2 = make_pr(
            repo_b, "foo2", [{'z': 'q'}],
            target="other",
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    time.sleep(5)
    env.run_crons()

    pr1_a_id = to_pr(env, pr1_a)
    pr1_b_id = to_pr(env, pr1_b)
    assert not pr1_a_id.blocked and not pr1_b_id.blocked
    assert pr1_a_id.batch_id == pr1_b_id.batch_id
    pr2_id = to_pr(env, pr2)
    # cheat: we need to fixup the label to match the existing batch
    pr2_id.label = pr1_b_id.label
    assert not pr2_id.blocked

    with repo_b:
        pr2.base = 'master'
    assert pr1_a_id.target == pr2_id.target
    assert pr1_a_id.batch_id != pr2_id.batch_id

def test_stage_different_statuses(env, project, repo_a, repo_b, config):
    project.batch_limit = 1

    env['runbot_merge.repository'].search([
        ('name', '=', repo_b.name)
    ]).write({
        'required_statuses': 'foo/bar',
    })

    with repo_a:
        repo_a.make_commits(None, Commit('initial', tree={'a': 'a_0'}), ref='heads/master')
        pr_a = make_pr(
            repo_a, 'do-a-thing', [{'a': 'a_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
        repo_a.post_status(pr_a.head, 'success', 'foo/bar')
    with repo_b:
        repo_b.make_commits(None, Commit('initial', tree={'a': 'b_0'}), ref='heads/master')
        [c] = repo_b.make_commits(
            'heads/master',
            repo_b.Commit(f'some_commit\n\nSee also {repo_a.name}#{pr_a.number:d}', tree={'a': 'b_1'}),
            ref='heads/do-a-thing'
        )
        pr_b = repo_b.make_pr(
            title="title", body="body", target='master', head='do-a-thing',
            token=config['role_user']['token'])
        repo_b.post_status(c, 'success')
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
            repo.post_status('staging.master', 'success')
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
        repo_a.make_commits(None, Commit('initial', tree={'a': 'a_0'}), ref='heads/master')
        pr_a = make_pr(
            repo_a, 'patch-1', [{'a': 'a_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    with repo_b:
        repo_b.make_commits(None, Commit('initial', tree={'a': 'b_0'}), ref=f'heads/master')
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
        repo_a.make_commits(None, Commit('initial', tree={'a': 'a_0'}), ref='heads/master')
    with repo_b:
        repo_b.make_commits(None, Commit('initial', tree={'a': 'b_0'}), ref='heads/master')
        pr_b = make_pr(
            repo_b, 'do-a-thing', [{'a': 'b_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    with repo_c:
        repo_c.make_commits(None, Commit('initial', tree={'a': 'c_0'}), ref='heads/master')
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
    assert sorted(st.head_ids.mapped('sha')) == sorted([
        a_staging.id,
        b_staging.id,
        c_staging.id,
    ])
    s = env['runbot_merge.stagings'].for_heads(
        a_staging.id,
        b_staging.id,
        c_staging.id,
    )
    assert s == list(st.ids)

    assert sorted(st.commit_ids.mapped('sha')) == sorted([
        b_staging.id,
        c_staging.id,
    ])
    s = env['runbot_merge.stagings'].for_commits(
        b_staging.id,
        c_staging.id,
    )
    assert s == list(st.ids)


def test_merge_fail(env, project, repo_a, repo_b, users, config):
    """ In a matched-branch scenario, if merging in one of the linked repos
    fails it should revert the corresponding merges
    """
    project.batch_limit = 1

    with repo_a, repo_b:
        repo_a.make_commits(None, Commit('initial', tree={'a': 'a_0'}), ref='heads/master')
        repo_b.make_commits(None, Commit('initial', tree={'a': 'b_0'}), ref='heads/master')

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
        repo_b.make_commits('heads/master', Commit('cn', tree={'a': 'cn'}), ref='heads/master', make=False)

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
        (users['user'], '@%(user)s @%(reviewer)s unable to stage: merge conflict' % users),
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
        [root_a] = repo_a.make_commits(None, Commit('initial', tree={'a': 'a_0'}), ref='heads/master')
        make_pr(
            repo_a, 'do-a-thing', [{'a': 'a_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )

        repo_b.make_commits(None, Commit('initial', tree={'a': 'b_0'}), ref=f'heads/master')
        make_pr(
            repo_b, 'do-a-thing', [{'a': 'b_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    env.run_crons()

    # add second commit blocking FF
    with repo_b:
        [cn] = repo_b.make_commits('heads/master', Commit('second', tree={'b': 'other'}), ref='heads/master')
    assert repo_b.commit('heads/master').id == cn

    with repo_a, repo_b:
        repo_a.post_status('heads/staging.master', 'success')
        repo_b.post_status('heads/staging.master', 'success')
    env.run_crons(None)
    assert repo_b.commit('heads/master').id == cn,\
        "B should still be at the conflicting commit"
    assert repo_a.commit('heads/master').id == root_a,\
        "FF A should have been rolled back when B failed"

    # should be re-staged
    st = env['runbot_merge.stagings'].search([])
    assert len(st) == 1
    assert len(st.batch_ids.prs) == 2

def test_ff_fail_second_step(env, project, repo_a, repo_b, config, users):
    """In a matched branch scenario, normally the safety_dance / ff dry run on
    tmp handles FF errors, however it's possible for that to succeed then for
    the "real" fast-forward to fail e.g. if github has transient errors.

    We need to handle this special state, surface it, and allow recovery
    because the branch might be partially updated:

    - mark batches we have fully forward ported as merged
    - put the partially ported batches in error
    - disable staging on the branch
    - send some sort of notification to admins?
    """
    # region setup
    with repo_a, repo_b:
        [a1] = repo_a.make_commits(None, Commit('initial', tree={'a': 'a_0'}), ref='heads/master')
        [b1, _b2] = repo_b.make_commits(
            None,
            Commit('first', tree={'a': 'a_0'}),
            Commit('second', tree={'a': 'a_1'}),
            ref='heads/master',
        )

    with repo_a, repo_b:
        repo_a.make_commits('master', Commit("Batch A", tree={'a': 'a_1'}), ref='heads/mybranch')
        pr_a = repo_a.make_pr(target='master', head='mybranch')
        pr_a.post_comment('hansen r+', config['role_reviewer']['token'])
        repo_a.post_status('heads/mybranch', 'success')

        repo_b.make_commits('master', Commit('Batch B', tree={'a': 'b_1'}), ref='heads/mybranch2')
        pr_b = repo_b.make_pr(target='master', head='mybranch2')
        pr_b.post_comment('hansen r+', config['role_reviewer']['token'])
        repo_b.post_status('heads/mybranch2', 'success')

        repo_a.make_commits('master', Commit('Batch AB', tree={'xxx': "1"}), ref='heads/cross')
        repo_b.make_commits('master', Commit('Batch AB', tree={'xxx': "1"}), ref='heads/cross')
        pr_ab_a = repo_a.make_pr(target='master', head='cross')
        pr_ab_b = repo_b.make_pr(target='master', head='cross')
        pr_ab_a.post_comment('hansen r+', config['role_reviewer']['token'])
        pr_ab_b.post_comment('hansen r+', config['role_reviewer']['token'])
        repo_a.post_status('heads/cross', 'success')
        repo_b.post_status('heads/cross', 'success')
    env.run_crons()

    with repo_a, repo_b:
        repo_a.post_status('heads/staging.master', 'success')
        repo_b.post_status('heads/staging.master', 'success')
        repo_b.update_ref('heads/master', b1, force=True)
    staging = env['runbot_merge.stagings'].search([])
    assert staging
    # endregion

    # rollback the target branch on repo_b, to trigger the branch rules: rules
    # don't trigger on a no-op, though that can still fail if github is on fire,
    # so we need to emulate it
    r = repo_b._get_session(None).post(
        f"https://api.github.com/repos/{repo_b.name}/rulesets",
        json={
            "name": "Prevent push to repo b",
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [],
            "conditions": {
                "ref_name": {
                    "include": ["~ALL"],
                    "exclude": ["refs/heads/staging.*", "refs/heads/tmp.*"],
                }
            },
            "rules": [{"type": "update"}],
        }
    )
    r.raise_for_status()

    env.run_crons()
    assert staging.state == 'failure'

    # project state is now inconsistent
    assert [c['commit']['message'].split('\n')[0] for c in repo_a.log('master')] \
       == ['Batch AB', 'Batch A', 'initial']
    assert [c['commit']['message'].split('\n')[0] for c in repo_b.log('master')] \
       == ['first']  # missing Batch B and Batch AB

    assert project.branch_ids.staging_enabled is False, \
        "branch should have been disabled"
    # since the error was on repo b, batches on just repo a should be marked merged
    assert to_pr(env, pr_a).state == 'merged'
    # batches on just b should be unmarked (they're basically unaffected)
    assert to_pr(env, pr_b).state == "ready"
    # batches on both should be in error
    assert to_pr(env, pr_ab_a).state == "error"
    assert to_pr(env, pr_ab_b).state == "error"
    assert pr_ab_a.comments == [
        (users['reviewer'], "hansen r+"),
        seen(env, pr_ab_a, users),
        (users['user'], "@{user} @{reviewer} staging failed: inconsistent integration - succeeded for {repo_a} but failed for {repo_b}.".format(
            **users,
            repo_a=repo_a.name,
            repo_b=repo_b.name,
        )),
    ]

    assert staging.message_ids[::-1].mapped(
        lambda m: m.body.strip() or list(map(read_tracking_value, m.tracking_value_ids))
    ) == [
        "<p>A set of batches being tested for integration created</p>",
        [('state', 'Pending', 'Success')],
        "<p>Staging on master has been disabled pending investigation.</p>",
        [
            ('active', 1, 0),
            ('reason', False, f'inconsistent integration - succeeded for {repo_a.name} but failed for {repo_b.name}.'),
            ('state', 'Success', 'Failure'),
        ],
    ]

class TestCompanionsNotReady:
    def test_one_pair(self, env, project, repo_a, repo_b, config, users):
        """ If the companion of a ready branch-matched PR is not ready,
        they should not get staged
        """
        project.batch_limit = 1
        with repo_a, repo_b:
            repo_a.make_commits(None, Commit('initial', tree={'a': 'a_0'}), ref='heads/master')
            # pr_a is born ready
            p_a = make_pr(
                repo_a, 'do-a-thing', [{'a': 'a_1'}],
                user=config['role_user']['token'],
                reviewer=config['role_reviewer']['token'],
            )

            repo_b.make_commits(None, Commit('initial', tree={'a': 'b_0'}), ref='heads/master')
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
            (users['user'], "@%s @%s linked pull request(s) %s not ready. Linked PRs are not staged until all of them are ready." % (
                users['user'],
                users['reviewer'],
                pr_b.display_name,
            )),
        ]
        # ensure the message is only sent once per PR
        env.run_crons('runbot_merge.check_linked_prs_status')
        assert p_a.comments == [
            (users['reviewer'], 'hansen r+'),
            seen(env, p_a, users),
            (users['user'], "@%s @%s linked pull request(s) %s not ready. Linked PRs are not staged until all of them are ready." % (
                users['user'],
                users['reviewer'],
                pr_b.display_name,
            )),
        ]
        assert p_b.comments == [seen(env, p_b, users)]

    def test_two_of_three_unready(self, env, project, repo_a, repo_b, repo_c, users, config):
        """ In a 3-batch, if two of the PRs are not ready both should be
        linked by the first one
        """
        project.batch_limit = 1
        with repo_a, repo_b, repo_c:
            repo_a.make_commits(None, Commit('initial', tree={'f': 'a0'}), ref='heads/master')
            pr_a = make_pr(
                repo_a, 'a-thing', [{'f': 'a1'}],
                user=config['role_user']['token'],
                reviewer=None,
            )

            repo_b.make_commits(None, Commit('initial', tree={'f': 'b0'}), ref='heads/master')
            pr_b = make_pr(
                repo_b, 'a-thing', [{'f': 'b1'}],
                user=config['role_user']['token'],
                reviewer=config['role_reviewer']['token'],
            )

            repo_c.make_commits(None, Commit('initial', tree={'f': 'c0'}), ref='heads/master')
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
            (users['user'], "@%s @%s linked pull request(s) %s#%d, %s#%d not ready. Linked PRs are not staged until all of them are ready." % (
                users['user'], users['reviewer'],
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
            repo_a.make_commits(None, Commit('initial', tree={'f': 'a0'}), ref='heads/master')
            pr_a = make_pr(
                repo_a, 'a-thing', [{'f': 'a1'}],
                user=config['role_user']['token'],
                reviewer=None,
            )

            repo_b.make_commits(None, Commit('initial', tree={'f': 'b0'}), ref='heads/master')
            pr_b = make_pr(
                repo_b, 'a-thing', [{'f': 'b1'}],
                user=config['role_user']['token'],
                reviewer=config['role_reviewer']['token'],
            )

            repo_c.make_commits(None, Commit('initial', tree={'f': 'c0'}), ref='heads/master')
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
            (users['user'], f"@{users['user']} @{users['reviewer']} linked pull request(s) {repo_a.name}#{pr_a.number} not ready. Linked PRs are not staged until all of them are ready.")
        ]
        assert pr_c.comments == [
            (users['reviewer'], 'hansen r+'),
            seen(env, pr_c, users),
            (users['user'],
             f"@{users['user']} @{users['reviewer']} linked pull request(s) {repo_a.name}#{pr_a.number} not ready. Linked PRs are not staged until all of them are ready.")
        ]

def test_other_failed(env, project, repo_a, repo_b, users, config):
    """ In a non-matched-branch scenario, if the companion staging (copy of
    targets) fails when built with the PR, it should provide a non-useless
    message
    """
    with repo_a, repo_b:
        repo_a.make_commits(None, Commit('initial', tree={'a': 'a_0'}), ref='heads/master')
        # pr_a is born ready
        pr_a = make_pr(
            repo_a, 'do-a-thing', [{'a': 'a_1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )

        repo_b.make_commits(None, Commit('initial', tree={'a': 'b_0'}), ref='heads/master')
    env.run_crons()

    pr = to_pr(env, pr_a)
    assert pr.staging_id

    with repo_a, repo_b:
        repo_a.post_status('heads/staging.master', 'success', target_url="http://example.org/a")
        repo_b.post_status('heads/staging.master', 'failure', target_url="http://example.org/b")
    env.run_crons()

    sth = repo_b.commit('heads/staging.master').id
    assert not pr.staging_id
    assert pr.state == 'error'
    assert pr_a.comments == [
        (users['reviewer'], 'hansen r+'),
        seen(env, pr_a, users),
        (users['user'], '@%s @%s staging failed: default on %s (view more at http://example.org/b)' % (
            users['user'], users['reviewer'],
            sth
        ))
    ]

class TestMultiBatches:
    def test_batching(self, env, project, repo_a, repo_b, config):
        """ If multiple batches (label groups) are ready they should get batched
        together (within the limits of teh project's batch limit)
        """
        project.batch_limit = 3

        with repo_a, repo_b:
            repo_a.make_commits(None, Commit('initial', tree={'a': 'a0'}), ref='heads/master')
            repo_b.make_commits(None, Commit('initial', tree={'b': 'b0'}), ref='heads/master')

            prs = [(
                a and make_pr(repo_a, 'batch{}'.format(i), [{'a{}'.format(i): 'a{}'.format(i)}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token']),
                b and make_pr(repo_b, 'batch{}'.format(i), [{'b{}'.format(i): 'b{}'.format(i)}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token']),
            )
                for i, (a, b) in enumerate([(1, 1), (0, 1), (1, 1), (1, 1), (1, 0)])
            ]
        env.run_crons()
        prs = [
            (a and to_pr(env, a), b and to_pr(env, b))
            for (a, b) in prs
        ]

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
            repo_a.make_commits(None, Commit('initial', tree={'a': 'a0'}), ref='heads/master')
            repo_b.make_commits(None, Commit('initial', tree={'b': 'b0'}), ref='heads/master')

            prs = [(
                a and make_pr(repo_a, 'batch{}'.format(i), [{'a{}'.format(i): 'a{}'.format(i)}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token']),
                b and make_pr(repo_b, 'batch{}'.format(i), [{'b{}'.format(i): 'b{}'.format(i)}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token']),
            )
                for i, (a, b) in enumerate([(1, 1), (0, 1), (1, 1), (1, 1), (1, 0)])
            ]
        env.run_crons()
        prs = [
            (a and to_pr(env, a), b and to_pr(env, b))
            for (a, b) in prs
        ]

        st0 = env['runbot_merge.stagings'].search([])
        assert len(st0.batch_ids) == 5
        assert len(st0.mapped('batch_ids.prs')) == 8

        # mark b.staging as failed -> should create two splits with (0, 1)
        # and (2, 3, 4) and stage the first one
        with repo_b:
            repo_b.post_status('heads/staging.master', 'failure')
        env.run_crons()

        assert not st0.active

        # at this point we have a re-staged split and an unstaged split
        st = env['runbot_merge.stagings'].search([])
        sp = env['runbot_merge.split'].search([])
        assert st
        assert sp

        assert len(st.batch_ids) == 3
        assert st.mapped('batch_ids.prs') == \
            prs[0][0] | prs[0][1] | prs[1][1] | prs[2][0] | prs[2][1]

        assert len(sp.batch_ids) == 2
        assert sp.mapped('batch_ids.prs') == \
             prs[3][0] | prs[3][1] | prs[4][0]
@pytest.mark.usefixtures("reviewer_admin")
def test_urgent(env, repo_a, repo_b, config):
    """ Either PR of a co-dependent pair being prioritised leads to the entire
    pair being prioritized
    """
    with repo_a, repo_b:
        repo_a.make_commits(None, Commit('initial', tree={'a0': 'a'}), ref='heads/master')
        repo_b.make_commits(None, Commit('initial', tree={'b0': 'b'}), ref='heads/master')

        pr_a = make_pr(repo_a, 'batch', [{'a1': 'a'}, {'a2': 'a'}], user=config['role_user']['token'], reviewer=None, statuses=[])
        pr_b = make_pr(repo_b, 'batch', [{'b1': 'b'}, {'b2': 'b'}], user=config['role_user']['token'], reviewer=None, statuses=[])
        pr_c = make_pr(repo_a, 'C', [{'c1': 'c', 'c2': 'c'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])

        pr_a.post_comment('hansen rebase-merge', config['role_reviewer']['token'])
        pr_b.post_comment('hansen rebase-merge alone skipchecks', config['role_reviewer']['token'])
    env.run_crons()

    p_a, p_b, p_c = to_pr(env, pr_a), to_pr(env, pr_b), to_pr(env, pr_c)
    assert not p_a.blocked
    assert not p_b.blocked

    assert p_a.staging_id and p_b.staging_id and p_a.staging_id == p_b.staging_id,\
        "a and b should be staged despite neither beinbg reviewed or approved"
    assert p_a.batch_id and p_b.batch_id and p_a.batch_id == p_b.batch_id,\
        "a and b should have been recognised as co-dependent"
    assert not p_c.staging_id

    with repo_a:
        pr_a.post_comment('hansen r-', config['role_reviewer']['token'])
    env.run_crons()
    assert not p_b.staging_id.active, "should be unstaged"
    assert p_b.priority == 'alone', "priority should not be affected anymore"
    assert not p_b.skipchecks, "r- of linked pr should have un-skipcheck-ed this one"
    assert p_a.blocked
    assert p_b.blocked

class TestBlocked:
    def test_merge_method(self, env, repo_a, config):
        env.ref('runbot_merge.cron_validate').active = False
        with repo_a:
            repo_a.make_commits(None, Commit('initial', tree={'a0': 'a'}), ref='heads/master')

            pr = make_pr(repo_a, 'A', [{'a1': 'a'}, {'a2': 'a'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'],)
        env.run_crons()

        p = to_pr(env, pr)
        assert p.state == 'ready'
        assert not p.blocked
        assert not p.staging_id

        with repo_a: pr.post_comment('hansen rebase-merge', config['role_reviewer']['token'])
        env.run_crons()
        assert p.staging_id

    def test_linked_closed(self, env, repo_a, repo_b, config):
        with repo_a, repo_b:
            repo_a.make_commits(None, Commit('initial', tree={'a0': 'a'}), ref='heads/master')
            repo_b.make_commits(None, Commit('initial', tree={'b0': 'b'}), ref='heads/master')

            pr1_a = make_pr(repo_a, 'xxx', [{'a1': 'a'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'],)
            pr1_b = make_pr(repo_b, 'xxx', [{'b1': 'b'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'], statuses=[])
        env.run_crons()

        head_a = repo_a.commit('master').id
        head_b = repo_b.commit('master').id
        pr1_a_id = to_pr(env, pr1_a)
        pr1_b_id = to_pr(env, pr1_b)
        assert pr1_a_id.blocked
        with repo_b: pr1_b.close()
        assert not pr1_a_id.blocked
        assert len(pr1_a_id.batch_id.all_prs) == 2
        assert pr1_a_id.state == 'ready'
        assert pr1_b_id.state == 'closed'
        env.run_crons()
        assert pr1_a_id.staging_id
        with repo_a, repo_b:
            repo_a.post_status('staging.master', 'success')
            repo_b.post_status('staging.master', 'success')
        env.run_crons()
        assert pr1_a_id.state == 'merged'
        assert pr1_a_id.batch_id.merge_date
        assert repo_a.commit('master').id != head_a, \
            "the master of repo A should be updated"
        assert repo_b.commit('master').id == head_b, \
            "the master of repo B should not be updated"

        with repo_a:
            pr2_a = make_pr(repo_a, "xxx", [{'x': 'x'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        env.run_crons()
        pr2_a_id = to_pr(env, pr2_a)
        assert pr2_a_id.batch_id != pr1_a_id.batch_id
        assert pr2_a_id.label == pr1_a_id.label
        assert len(pr2_a_id.batch_id.all_prs) == 1

    def test_linked_merged(self, env, repo_a, repo_b, config):
        with repo_a, repo_b:
            repo_a.make_commits(None, Commit('initial', tree={'a0': 'a'}), ref='heads/master')
            repo_b.make_commits(None, Commit('initial', tree={'b0': 'b'}), ref='heads/master')

            b = make_pr(repo_b, 'xxx', [{'b1': 'b'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'],)
        env.run_crons() # stage b and c

        with repo_a, repo_b:
            repo_a.post_status('heads/staging.master', 'success')
            repo_b.post_status('heads/staging.master', 'success')
        env.run_crons() # merge b and c
        assert to_pr(env, b).state == 'merged'

        with repo_a:
            pr = make_pr(repo_a, 'xxx', [{'a1': 'a'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'],)
        env.run_crons() # merge b and c

        p = to_pr(env, pr)
        assert not p.blocked

    @pytest.mark.usefixtures("reviewer_admin")
    def test_linked_unready(self, env, repo_a, repo_b, config):
        """ Create a PR A linked to a non-ready PR B,
        * A is blocked by default
        * A is not blocked if A.skipci
        * A is not blocked if B.skipci
        """
        with repo_a, repo_b:
            repo_a.make_commits(None, Commit('initial', tree={'a0': 'a'}), ref='heads/master')
            repo_b.make_commits(None, Commit('initial', tree={'b0': 'b'}), ref='heads/master')

            a = make_pr(repo_a, 'xxx', [{'a1': 'a'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'],)
            b = make_pr(repo_b, 'xxx', [{'b1': 'b'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'], statuses=[])
        env.run_crons()

        pr_a = to_pr(env, a)
        assert pr_a.blocked

        with repo_a: a.post_comment('hansen skipchecks', config['role_reviewer']['token'])
        assert not pr_a.blocked
        pr_a.skipchecks = False

        with repo_b: b.post_comment('hansen skipchecks', config['role_reviewer']['token'])
        assert not pr_a.blocked

def test_different_branches(env, project, repo_a, repo_b, config):
    project.write({
        'branch_ids': [(0, 0, {'name': 'dev'})]
    })
    # repo_b only works with master
    env['runbot_merge.repository'].search([('name', '=', repo_b.name)])\
        .branch_filter = '[("name", "=", "master")]'
    with repo_a, repo_b:
        repo_a.make_commits(None, Commit('initial', tree={'a': '0'}), ref='heads/dev')
        repo_a.make_commits(None, Commit('initial', tree={'b': '0'}), ref='heads/master')
        repo_b.make_commits(None, Commit('initial', tree={'b': '0'}), ref='heads/master')

        pr_a = make_pr(
            repo_a, 'xxx', [{'a': '1'}],
            target='dev',
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token']
        )
    env.run_crons()

    with repo_a:
        pr_a.post_comment('hansen r+', config['role_reviewer']['token'])
        repo_a.post_status('heads/staging.dev', 'success')
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
            'github_name': "yyy",
            'github_email': "zzz@example.org",
            'repo_ids': [(0, 0, {'name': 'xxx/xxx'})],
            'branch_ids': [(0, 0, {'name': 'master'})]
        })
        env['runbot_merge.events_sources'].create({'repository': 'xxx/xxx'})
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
                    },
                    'sender': {'login': 'pytest'}
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
            repo_a.make_commits(None, Commit('initial', tree={'a': '0'}), ref='heads/master')
        with repo_b:
            repo_b.make_commits(None, Commit('initial', tree={'b': '0'}), ref='heads/master')

        # policy is that repo_a PRs are created in the same repo while repo_b PRs
        # are created in personal forks
        with repo_a:
            repo_a.make_commits('master', repo_a.Commit('bop', tree={'a': '1'}), ref='heads/abranch')
            pra = repo_a.make_pr(target='master', head='abranch')
        with repo_b, repo_b.fork() as b_fork:
            b_fork.make_commits(repo_b.commit('master').id, b_fork.Commit('pob', tree={'b': '1'}), ref='heads/abranch')
            prb = repo_b.make_pr(
                title="a pr",
                target='master', head='%s:abranch' % b_fork.owner
            )

        pra_id = to_pr(env, pra)
        prb_id = to_pr(env, prb)
        assert pra_id.label.endswith(':abranch')
        assert prb_id.label.endswith(':abranch')

        with repo_a, repo_b:
            repo_a.post_status(pra.head, 'success')
            pra.post_comment('hansen r+', config['role_reviewer']['token'])

            repo_b.post_status(prb.head, 'success')
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
        'github_name': config['github']['name'],
        'github_email': "foo@example.org",
        'repo_ids': [(0, 0, {
            'name': r1.name,
            'group_id': False,
            'required_statuses': 'a',
        })],
        'branch_ids': [(0, 0, {'name': 'default'})],
    })
    setreviewers(*p1.repo_ids)
    env['runbot_merge.events_sources'].create([{'repository': r1.name}])

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
        'github_name': config['github']['name'],
        'github_email': "foo@example.org",
        'repo_ids': [(0, 0, {
            'name': r2.name,
            'group_id': False,
            'required_statuses': 'a',
        })],
        'branch_ids': [(0, 0, {'name': 'default'})],
    })
    setreviewers(*p2.repo_ids)
    env['runbot_merge.events_sources'].create([{'repository': r2.name}])

    assert r1_dev.owner == r2_dev.owner

    with r1, r1_dev:
        r1_dev.make_commits(r1.commit('default').id, Commit('new', tree={'a': 'b'}), ref='heads/other')

        # create, validate, and approve pr1
        pr1 = r1.make_pr(title='pr 1', target='default', head=r1_dev.owner + ':other')
        r1.post_status(pr1.head, 'success', 'a')
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])

    with r2, r2_dev:
        r2_dev.make_commits(r2.commit('default').id, Commit('new', tree={'b': 'b'}), ref='heads/other')

        # create second PR with the same label *in a different project*, don't
        # approve it
        pr2 = r2.make_pr(title='pr 2', target='default', head=r2_dev.owner + ':other')
        r2.post_status(pr2.head, 'success', 'a')
    env.run_crons()

    pr1_id = to_pr(env, pr1)
    pr2_id = to_pr(env, pr2)

    assert pr1_id.state == 'ready' and not pr1_id.blocked
    assert pr2_id.state == 'validated'

    assert pr1_id.staging_id
    assert not pr2_id.staging_id

    assert pr1.comments == [
        (users['reviewer'], 'hansen r+'),
        seen(env, pr1, users),
    ]
    assert pr2.comments == [seen(env, pr2, users)]

def test_freeze_complete(env, project, repo_a, repo_b, repo_c, users, config):
    """ Tests the freeze wizard feature (aside from the UI):

    * have a project with 3 repos, and two branches (1.0 and master) each
    * have 2 PRs required for the freeze
    * prep 3 freeze PRs
    * prep 1 bump PR
    * trigger the freeze wizard
    * trigger it again (check that the same object is returned, there should
      only be one freeze per project at a time)
    * configure the freeze
    * check that it doesn't go through
    * merge required PRs
    * check that freeze goes through
    * check that reminder is shown
    * check that new branches are created w/ correct parent & commit info
    * check that a PRs (freeze and bump) are part of synthetic stagings so
      they're correctly accounted for in the change history
    """
    project.freeze_reminder = "Don't forget to like and subscribe"

    # have a project with 3 repos, and two branches (1.0 and master)
    project.branch_ids = [
        (1, project.branch_ids.id, {'sequence': 1}),
        (0, 0, {'name': '1.0', 'sequence': 2}),
    ]

    [
        (master_head_a, master_head_b, master_head_c),
        (pr_required_a, _, pr_required_c),
        (pr_rel_a, pr_rel_b, pr_rel_c),
        pr_bump_a,
        pr_other
    ] = setup_mess(repo_a, repo_b, repo_c)
    env.run_crons() # process the PRs

    release_prs = {
        repo_a.name: to_pr(env, pr_rel_a),
        repo_b.name: to_pr(env, pr_rel_b),
        repo_c.name: to_pr(env, pr_rel_c),
    }
    pr_bump_id = to_pr(env, pr_bump_a)
    # trigger the ~~tree~~ freeze wizard
    w = project.action_prepare_freeze()
    w2 = project.action_prepare_freeze()
    assert w == w2, "each project should only have one freeze wizard active at a time"
    assert w['res_model'] == 'runbot_merge.project.freeze'

    w_id = env[w['res_model']].browse([w['res_id']])
    assert w_id.branch_name == '1.1', "check that the forking incremented the minor by 1"
    assert len(w_id.release_pr_ids) == len(project.repo_ids), \
        "should ask for a many release PRs as we have repositories"

    # configure required PRs
    w_id.required_pr_ids = (to_pr(env, pr_required_a) | to_pr(env, pr_required_c)).ids
    # configure releases
    for r in w_id.release_pr_ids:
        r.pr_id = release_prs[r.repository_id.name].id
    w_id.release_pr_ids[-1].pr_id = to_pr(env, pr_other).id
    # configure bump
    assert not w_id.bump_pr_ids, "there is no bump pr by default"
    w_id.write({'bump_pr_ids': [
        (0, 0, {'repository_id': pr_bump_id.repository.id, 'pr_id': pr_bump_id.id})
    ]})
    r = w_id.action_freeze()
    assert r == w, "the freeze is not ready so the wizard should redirect to itself"
    assert w_id.errors == f"""\
* All release PRs must have the same label, found '{pr_rel_c.user}:release-1.1, {pr_other.user}:whocares'.
* 2 required PRs not ready."""
    w_id.release_pr_ids[-1].pr_id = release_prs[repo_c.name].id

    with repo_a:
        pr_required_a.post_comment('hansen r+', config['role_reviewer']['token'])
        repo_a.post_status(pr_required_a.head, 'success')
    with repo_c:
        pr_required_c.post_comment('hansen r+', config['role_reviewer']['token'])
        repo_c.post_status(pr_required_c.head, 'success')
    env.run_crons()

    for repo in [repo_a, repo_b, repo_c]:
        with repo:
            repo.post_status('staging.master', 'success')
    env.run_crons()

    assert to_pr(env, pr_required_a).state == 'merged'
    assert to_pr(env, pr_required_c).state == 'merged'

    assert not w_id.errors

    # assume the wizard is closed, re-open it
    w = project.action_prepare_freeze()
    assert w['res_model'] == 'runbot_merge.project.freeze'
    assert w['res_id'] == w_id.id, "check that we're still getting the old wizard"
    w_id = env[w['res_model']].browse([w['res_id']])
    assert w_id.exists()

    # actually perform the freeze
    r = w_id.action_freeze()
    # check that the wizard was deleted
    assert not w_id.exists()
    # check that the wizard pops out a reminder dialog (kinda)
    assert r['res_model'] == 'runbot_merge.project'
    assert r['res_id'] == project.id

    release_pr_ids = functools.reduce(operator.add, release_prs.values())
    # stuff that's done directly
    assert all(pr_id.state == 'merged' for pr_id in release_pr_ids)
    assert pr_bump_id.state == 'merged'
    assert pr_bump_id.commits_map != '{}'

    assert len(release_pr_ids.batch_id) == 1
    assert release_pr_ids.batch_id.merge_date
    assert release_pr_ids.batch_id.staging_ids.target.name == '1.1'
    assert release_pr_ids.batch_id.staging_ids.state == 'success'

    assert pr_bump_id.batch_id.merge_date
    assert pr_bump_id.batch_id.staging_ids.target.name == 'master'
    assert pr_bump_id.batch_id.staging_ids.state == 'success'

    # stuff that's behind a cron
    env.run_crons()

    # check again to be sure
    assert all(pr_id.state == 'merged' for pr_id in release_pr_ids)
    assert pr_bump_id.state == 'merged'

    assert pr_rel_a.state == "closed"
    assert pr_rel_a.base['ref'] == '1.1'
    assert pr_rel_b.state == "closed"
    assert pr_rel_b.base['ref'] == '1.1'
    assert pr_rel_c.state == "closed"
    assert pr_rel_c.base['ref'] == '1.1'
    assert all(pr_id.target.name == '1.1' for pr_id in release_pr_ids)

    assert pr_bump_a.state == 'closed'
    assert pr_bump_a.base['ref'] == 'master'
    assert pr_bump_id.target.name == 'master'

    m_a = repo_a.commit('master')
    assert m_a.message.startswith('Bump A')
    assert repo_a.read_tree(m_a) == {
        'f': '1', # from master
        'g': 'x', # from required PR (merged into master before forking)
        'version': '1.2-alpha', # from bump PR
    }

    c_a = repo_a.commit('1.1')
    assert c_a.message.startswith('Release 1.1 (A)')
    assert repo_a.read_tree(c_a) == {
        'f': '1', # from master
        'g': 'x', # from required pr
        'version': '1.1', # from release commit
    }
    c_a_parent = repo_a.commit(c_a.parents[0])
    assert c_a_parent.message.startswith('super important file')
    assert c_a_parent.parents[0] == master_head_a

    c_b = repo_b.commit('1.1')
    assert c_b.message.startswith('Release 1.1 (B)')
    assert repo_b.read_tree(c_b) == {'f': '1', 'version': '1.1'}
    assert c_b.parents[0] == master_head_b

    c_c = repo_c.commit('1.1')
    assert c_c.message.startswith('Release 1.1 (C)')
    assert repo_c.read_tree(c_c) == {'f': '2', 'version': '1.1'}
    assert repo_c.commit(c_c.parents[0]).parents[0] == master_head_c


def setup_mess(repo_a, repo_b, repo_c):
    master_heads = []
    for r in [repo_a, repo_b, repo_c]:
        with r:
            [root, _] = r.make_commits(
                None,
                Commit('base', tree={'version': '', 'f': '0'}),
                Commit('release 1.0', tree={'version': '1.0'}),
                ref='heads/1.0'
            )
            master_heads.extend(r.make_commits(root, Commit('other', tree={'f': '1'}), ref='heads/master'))

    a_fork = repo_a.fork()
    b_fork = repo_b.fork()
    c_fork = repo_c.fork()
    assert a_fork.owner == b_fork.owner == c_fork.owner
    owner = a_fork.owner
    # have 2 PRs required for the freeze
    with repo_a, a_fork:
        a_fork.make_commits(master_heads[0], Commit('super important file', tree={'g': 'x'}), ref='heads/apr')
        pr_required_a = repo_a.make_pr(target='master', head=f'{owner}:apr', title="xxx")
    with repo_c, c_fork:
        c_fork.make_commits(master_heads[2], Commit('update thing', tree={'f': '2'}), ref='heads/cpr')
        pr_required_c = repo_c.make_pr(target='master', head=f'{owner}:cpr', title="yyy")
    # have 3 release PRs, only the first one updates the tree (version file)
    with repo_a, a_fork:
        a_fork.make_commits(
            master_heads[0],
            Commit('Release 1.1 (A)', tree={'version': '1.1'}),
            ref='heads/release-1.1'
        )
        pr_rel_a = repo_a.make_pr(target='master', head=f'{owner}:release-1.1', title="zzz")
    with repo_b, b_fork:
        b_fork.make_commits(
            master_heads[1],
            Commit('Release 1.1 (B)', tree={'version': '1.1'}),
            ref='heads/release-1.1'
        )
        pr_rel_b = repo_b.make_pr(target='master', head=f'{owner}:release-1.1', title="000")
    with repo_c, c_fork:
        c_fork.make_commits(master_heads[2], Commit("Some change", tree={'a': '1'}), ref='heads/whocares')
        pr_other = repo_c.make_pr(target='master', head=f'{owner}:whocares', title="111")
        c_fork.make_commits(
            master_heads[2],
            Commit('Release 1.1 (C)', tree={'version': '1.1'}),
            ref='heads/release-1.1'
        )
        pr_rel_c = repo_c.make_pr(target='master', head=f'{owner}:release-1.1', title="222")
    # have one bump PR on repo A
    with repo_a, a_fork:
        a_fork.make_commits(
            master_heads[0],
            Commit("Bump A", tree={'version': '1.2-alpha'}),
            ref='heads/bump-1.1',
        )
        pr_bump_a = repo_a.make_pr(target='master', head=f'{owner}:bump-1.1', title="333")
    return master_heads, (pr_required_a, None, pr_required_c), (pr_rel_a, pr_rel_b, pr_rel_c), pr_bump_a, pr_other

def test_freeze_subset(env, project, repo_a, repo_b, repo_c, users, config):
    """It should be possible to only freeze a subset of a project when e.g. one
    of the repository is managed differently than the rest and has
    non-synchronous releases.

    - it should be possible to mark repositories as non-freezed (just opted out
      of the entire thing), in which case no freeze PRs should be asked of them
    - it should be possible to remove repositories from the freeze wizard
    - repositories which are not in the freeze wizard should just not be frozen

    To do things correctly that should probably match with the branch filters
    and stuff, but that's a configuration concern.
    """
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

    with repo_a:
        repo_a.make_commits(
            masters[0],
            Commit('Release 1.1', tree={'version': '1.1'}),
            ref='heads/release-1.1'
        )
        pr_rel_a = repo_a.make_pr(target='master', head='release-1.1')

    # the third repository we opt out of freezing
    project.repo_ids.filtered(lambda r: r.name == repo_c.name).freeze = False
    env.run_crons() # process the PRs

    # open the freeze wizard
    w = project.action_prepare_freeze()
    w_id = env[w['res_model']].browse([w['res_id']])
    # check that there are only rels for repos A and B
    assert w_id.mapped('release_pr_ids.repository_id.name') == [repo_a.name, repo_b.name]
    # remove B from the set
    b_id = w_id.release_pr_ids.filtered(lambda r: r.repository_id.name == repo_b.name)
    w_id.write({'release_pr_ids': [(3, b_id.id, 0)]})
    assert len(w_id.release_pr_ids) == 1
    # set lone release PR
    w_id.release_pr_ids.pr_id = to_pr(env, pr_rel_a).id
    assert not w_id.errors

    w_id.action_freeze()
    assert not w_id.exists()

    assert repo_a.commit('1.1'), "should have created branch in repo A"
    try:
        repo_b.commit('1.1')
        pytest.fail("should *not* have created branch in repo B")
    except AssertionError:
        ...
    try:
        repo_c.commit('1.1')
        pytest.fail("should *not* have created branch in repo C")
    except AssertionError:
        ...
    # can't stage because we (wilfully) don't have branches 1.1 in repos B and C

@pytest.mark.skip("env's session is not thread-safe sadface")
def test_race_conditions():
    """need the ability to dup the env in order to send concurrent requests to
    the inner odoo

    - try to run the action_freeze during a cron (merge or staging), should
      error (recover and return nice message?)
    - somehow get ahead of the action and update master's commit between moment
      where it is fetched and moment where the bump pr is fast-forwarded,
      there's actually a bit of time thanks to the rate limiting (fetch of base,
      update of tmp to base, rebase of commits on tmp, wait 1s, for each release
      and bump PR, then the release branches are created, and finally the bump
      prs)
    """
    ...

def test_freeze_conflict(env, project, repo_a, repo_b, repo_c, users, config):
    """If one of the branches we're trying to create already exists, the wizard
    fails.
    """
    project.branch_ids = [
        (1, project.branch_ids.id, {'sequence': 1}),
        (0, 0, {'name': '1.0', 'sequence': 2}),
    ]
    heads, _, (pr_rel_a, pr_rel_b, pr_rel_c), bump, other = \
        setup_mess(repo_a, repo_b, repo_c)
    env.run_crons()

    release_prs = {
        repo_a.name: to_pr(env, pr_rel_a),
        repo_b.name: to_pr(env, pr_rel_b),
        repo_c.name: to_pr(env, pr_rel_c),
    }

    w = project.action_prepare_freeze()
    w_id = env[w['res_model']].browse([w['res_id']])
    for repo, release_pr in release_prs.items():
        w_id.release_pr_ids\
            .filtered(lambda r: r.repository_id.name == repo)\
            .pr_id = release_pr.id

    # create conflicting branch
    with repo_c:
        [c] = repo_c.make_commits(heads[2], Commit("exists", tree={'version': ''}))
        repo_c.make_ref('heads/1.1', c)

    # actually perform the freeze
    with pytest.raises(xmlrpc.client.Fault) as e:
        w_id.action_freeze()
    assert f"Unable to create branch {repo_c.name}:1.1" in e.value.faultString

    # branches a and b should have been deleted
    with pytest.raises(AssertionError) as e:
        repo_a.get_ref('heads/1.1')
    assert e.value.args[0].startswith("Not Found")
    with pytest.raises(AssertionError) as e:
        repo_b.get_ref('heads/1.1')
    assert e.value.args[0].startswith("Not Found")

def test_cancel_staging(env, project, repo_a, repo_b, users, config):
    """If a batch is flagged as staging cancelling (from any PR), the staging
    should get cancelled if and when the batch transitions to unblocked
    """
    with repo_a, repo_b:
        repo_a.make_commits(None, Commit('initial', tree={'a': '1'}), ref='heads/master')
        repo_b.make_commits(None, Commit('initial', tree={'b': '1'}), ref='heads/master')

        pr_a = make_pr(repo_a, 'batch', [{'a': '2'}], user=config['role_user']['token'], statuses=[], reviewer=None)
        pr_b = make_pr(repo_b, 'batch', [{'b': '2'}], user=config['role_user']['token'], statuses=[], reviewer=None)
        pr_lone = make_pr(
            repo_a,
            "C",
            [{'c': '1'}],
            user=config['role_user']['token'],
            reviewer=config['role_reviewer']['token'],
        )
    env.run_crons()

    a_id, b_id, lone_id = map(to_pr, repeat(env), [pr_a, pr_b, pr_lone])
    assert lone_id.staging_id
    st = lone_id.staging_id

    with repo_a:
        pr_a.post_comment("hansen cancel=staging", config['role_reviewer']['token'])
    assert a_id.state == 'opened'
    assert a_id.cancel_staging
    assert b_id.cancel_staging
    assert lone_id.staging_id == st
    with repo_a:
        pr_a.post_comment('hansen r+', config['role_reviewer']['token'])
    assert a_id.state == 'approved'
    assert lone_id.staging_id == st
    with repo_a:
        repo_a.post_status(a_id.head, 'success')
    env.run_crons()
    assert a_id.state == 'ready'
    assert lone_id.staging_id == st

    assert b_id.state == 'opened'
    with repo_b:
        pr_b.post_comment('hansen r+', config['role_reviewer']['token'])
    assert b_id.state == 'approved'
    assert lone_id.staging_id == st
    with repo_b:
        repo_b.post_status(b_id.head, 'success')
    assert b_id.state == 'approved'
    assert lone_id.staging_id == st
    env.run_crons()
    assert b_id.state == 'ready'
    # should have cancelled the staging, picked a and b, and re-staged the
    # entire thing
    assert lone_id.staging_id != st

    assert len({
        lone_id.staging_id.id,
        a_id.staging_id.id,
        b_id.staging_id.id,
    }) == 1
