import datetime
import json

import pytest

from utils import get_partner, Commit, to_pr, part_of, ensure_one, node, log_to_node


def _pr(repo, prefix, trees, *, target='master', user, reviewer, statuses=(('default', 'success'),), cmd=''):
    """ Helper creating a PR from a series of commits on a base
    """
    *_, c = repo.make_commits(
        'heads/{}'.format(target),
        *(
            repo.Commit(f'commit_{prefix}_{i:02}', tree=t)
            for i, t in enumerate(trees)
        ),
        ref=f'heads/{prefix}'
    )
    pr = repo.make_pr(title=f'title {prefix}', body=f'body {prefix}',
                      target=target, head=prefix, token=user)

    for context, result in statuses:
        repo.post_status(c, result, context)
    if reviewer:
        command = f'rebase-merge' if len(trees) > 1 else ''
        pr.post_comment(f'hansen r+ {cmd} {command}'.rstrip(), reviewer)
    return pr

def test_staging_batch(env, repo, users, config):
    """ If multiple PRs are ready for the same target at the same point,
    they should be staged together
    """
    with repo:
        repo.make_commits(None, Commit('initial', tree={'a': 'some content'}), ref='heads/master')

        pr1 = _pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        pr2 = _pr(repo, 'PR2', [{'c': 'CCC'}, {'d': 'DDD'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
    env.run_crons()

    pr1 = to_pr(env, pr1)
    assert pr1.staging_id
    pr2 = to_pr(env, pr2)
    assert pr1.staging_id
    assert pr2.staging_id
    assert pr1.staging_id == pr2.staging_id

    log = list(repo.log('staging.master'))
    staging = log_to_node(log)
    reviewer = get_partner(env, users["reviewer"]).formatted_email
    p1 = node(
        'title PR1\n\nbody PR1\n\ncloses {}\n\nSigned-off-by: {}'.format(pr1.display_name, reviewer),
        node('initial'),
        node(part_of('commit_PR1_01', pr1), node(part_of('commit_PR1_00', pr1), node('initial')))
    )
    p2 = node(
        'title PR2\n\nbody PR2\n\ncloses {}\n\nSigned-off-by: {}'.format(pr2.display_name, reviewer),
        p1,
        node(part_of('commit_PR2_01', pr2), node(part_of('commit_PR2_00', pr2), p1))
    )
    assert staging == p2

def test_staging_batch_norebase(env, repo, users, config):
    """ If multiple PRs are ready for the same target at the same point,
    they should be staged together
    """
    with repo:
        repo.make_commits(None, Commit('initial', tree={'a': 'some content'}), ref='heads/master')

        pr1 = _pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        pr1.post_comment('hansen merge', config['role_reviewer']['token'])
        pr2 = _pr(repo, 'PR2', [{'c': 'CCC'}, {'d': 'DDD'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        pr2.post_comment('hansen merge', config['role_reviewer']['token'])
    env.run_crons()

    pr1 = to_pr(env, pr1)
    assert pr1.staging_id
    assert pr1.merge_method == 'merge'
    pr2 = to_pr(env, pr2)
    assert pr2.merge_method == 'merge'
    assert pr1.staging_id
    assert pr2.staging_id
    assert pr1.staging_id == pr2.staging_id

    log = list(repo.log('staging.master'))

    staging = log_to_node(log)
    reviewer = get_partner(env, users["reviewer"]).formatted_email

    p1 = node(
        'title PR1\n\nbody PR1\n\ncloses {}#{}\n\nSigned-off-by: {}'.format(repo.name, pr1.number, reviewer),
        node('initial'),
        node('commit_PR1_01', node('commit_PR1_00', node('initial')))
    )
    p2 = node(
        'title PR2\n\nbody PR2\n\ncloses {}#{}\n\nSigned-off-by: {}'.format(repo.name, pr2.number, reviewer),
        p1,
        node('commit_PR2_01', node('commit_PR2_00', node('initial')))
    )
    assert staging == p2

def test_staging_batch_squash(env, repo, users, config):
    """ If multiple PRs are ready for the same target at the same point,
    they should be staged together
    """
    with repo:
        repo.make_commits(None, Commit('initial', tree={'a': 'some content'}), ref="heads/master")

        pr1 = _pr(repo, 'PR1', [{'a': 'AAA'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        pr2 = _pr(repo, 'PR2', [{'c': 'CCC'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
    env.run_crons()

    pr1 = to_pr(env, pr1)
    assert pr1.staging_id
    pr2 = to_pr(env, pr2)
    assert pr1.staging_id
    assert pr2.staging_id
    assert pr1.staging_id == pr2.staging_id

    log = list(repo.log('staging.master'))

    staging = log_to_node(log)
    reviewer = get_partner(env, users["reviewer"]).formatted_email
    expected = node('commit_PR2_00\n\ncloses {}#{}\n\nSigned-off-by: {}'.format(repo.name, pr2.number, reviewer),
         node('commit_PR1_00\n\ncloses {}#{}\n\nSigned-off-by: {}'.format(repo.name, pr1.number, reviewer),
              node('initial')))
    assert staging == expected

def test_batching_pressing(env, repo, config):
    """ "Pressing" PRs should be selected before normal & batched together
    """
    # by limiting the batch size to 3 we allow both high-priority PRs, but
    # a single normal priority one
    env['runbot_merge.project'].search([]).batch_limit = 3
    with repo:
        repo.make_commits(None, Commit('initial', tree={'a': 'some content'}), ref="heads/master")

        pr21 = _pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        pr22 = _pr(repo, 'PR2', [{'c': 'CCC'}, {'d': 'DDD'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])

        pr11 = _pr(repo, 'Pressing1', [{'x': 'x'}, {'y': 'y'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        pr12 = _pr(repo, 'Pressing2', [{'z': 'z'}, {'zz': 'zz'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        pr11.post_comment('hansen priority', config['role_reviewer']['token'])
        pr12.post_comment('hansen priority', config['role_reviewer']['token'])
    # necessary to project commit statuses onto PRs
    env.run_crons()

    pr21, pr22, pr11, pr12 = prs = [to_pr(env, pr) for pr in [pr21, pr22, pr11, pr12]]
    assert pr11.priority == pr12.priority == 'priority'
    assert pr21.priority == pr22.priority == 'default'
    assert all(pr.state == 'ready' for pr in prs)

    staging = ensure_one(env['runbot_merge.stagings'].search([]))
    assert staging.pr_ids == pr11 | pr12 | pr21
    assert list(staging.batch_ids) == [
        pr11.batch_id,
        pr12.batch_id,
        pr21.batch_id,
    ]
    assert not pr22.staging_id

def test_prioritisation(env, repo, config, project):
    """Between priority=default batches, we should select the oldest
    modified, then the oldest created.
    """
    project.batch_limit = 1

    with repo:
        repo.make_commits(None, Commit('initial', tree={'a': 'some content'}), ref="heads/master")

        pr1 = _pr(repo, 'PR1', [{'a': 'AAA'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        pr2 = _pr(repo, 'PR2', [{'c': 'CCC'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
    pr1_id = to_pr(env, pr1)
    pr2_id = to_pr(env, pr2)

    env.run_crons()

    assert pr1_id.staging_id
    assert not pr2_id.staging_id

    project.branch_ids.active_staging_id\
        .cancel("See if ordering change works")

    # cheat by updating pr1's `unblocked_at` to make it later than pr2's
    batch1 = pr1_id.batch_id
    batch1.unblocked_at = datetime.datetime.now().isoformat(" ", "seconds")
    w1 = batch1.unblocked_at
    w2 = pr2_id.batch_id.unblocked_at
    assert w1 > w2

    env.run_crons()

    assert pr2_id.staging_id, "the PR with the earliest unblocking should be prioritised"
    assert not pr1_id.staging_id

def test_nice(env, repo, config):
    """Default PRs should be selected over nice even if nice PRs are older.
    """
    env['runbot_merge.project'].search([]).batch_limit = 1

    with repo:
        repo.make_commits(None, Commit('initial', tree={'a': 'some content'}), ref="heads/master")

        pr1 = _pr(repo, 'PR1', [{'a': 'AAA'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'], cmd='nice')
        pr2 = _pr(repo, 'PR2', [{'c': 'CCC'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
    pr1_id = to_pr(env, pr1)
    pr2_id = to_pr(env, pr2)

    env.run_crons()

    assert pr2_id.staging_id
    assert not pr1_id.staging_id

def test_batching_default(env, repo, config):
    """Nice prs should be selected after normal & batched together
    """
    proj = env['runbot_merge.project'].search([])
    proj.batch_limit = 3

    with repo:
        repo.make_commits(None, Commit('initial', tree={'a': 'some content'}), ref="heads/master")

        pr1 = _pr(repo, 'PR1', [{'a': 'AAA'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        pr2 = _pr(repo, 'PR2', [{'c': 'CCC'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
    env.run_crons()

    assert proj.branch_ids.active_staging_id.batch_ids \
       == to_pr(env, pr1).batch_id + to_pr(env, pr2).batch_id

def test_batching_nice(env, repo, config):
    """Nice prs should be selected after normal & batched together
    """
    proj = env['runbot_merge.project'].search([])
    proj.batch_limit = 3

    with repo:
        repo.make_commits(None, Commit('initial', tree={'a': 'some content'}), ref="heads/master")

        pr1 = _pr(repo, 'PR1', [{'a': 'AAA'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'], cmd='nice')
        pr2 = _pr(repo, 'PR2', [{'c': 'CCC'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
    env.run_crons()

    assert proj.branch_ids.active_staging_id.batch_ids \
       == to_pr(env, pr2).batch_id + to_pr(env, pr1).batch_id

@pytest.mark.usefixtures("reviewer_admin")
def test_batching_urgent(env, repo, config):
    with repo:
        repo.make_commits(None, Commit('initial', tree={'a': 'some content'}), ref="heads/master")

        pr11 = _pr(repo, 'Pressing1', [{'x': 'x'}, {'y': 'y'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        pr12 = _pr(repo, 'Pressing2', [{'z': 'z'}, {'zz': 'zz'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        pr11.post_comment('hansen NOW', config['role_reviewer']['token'])
        pr12.post_comment('hansen NOW', config['role_reviewer']['token'])

    # stage current PRs
    env.run_crons()
    p_11, p_12 = \
        [to_pr(env, pr) for pr in [pr11, pr12]]
    sm_all = p_11 | p_12
    staging_1 = sm_all.staging_id
    assert staging_1
    assert len(staging_1) == 1
    assert list(staging_1.batch_ids) == [
        p_11.batch_id,
        p_12.batch_id,
    ]

    # no statuses run on PR0s
    with repo:
        pr01 = _pr(repo, 'Urgent1', [{'n': 'n'}, {'o': 'o'}], user=config['role_user']['token'], reviewer=None, statuses=[])
        pr01.post_comment('hansen NOW! rebase-merge', config['role_reviewer']['token'])
    p_01 = to_pr(env, pr01)
    assert p_01.state == 'ready'
    assert p_01.priority == 'alone'
    assert p_01.skipchecks == True

    env.run_crons()
    # first staging should be cancelled and PR0 should be staged
    # regardless of CI (or lack thereof)
    assert not staging_1.active
    assert not p_11.staging_id and not p_12.staging_id
    assert p_01.staging_id
    assert p_11.state == 'ready'
    assert p_12.state == 'ready'

    # make the staging fail
    with repo:
        repo.post_status('staging.master', 'failure')
    env.run_crons()
    assert p_01.error
    assert p_01.batch_id.blocked
    assert p_01.blocked

    assert p_01.state == 'error'
    assert not p_01.staging_id.active
    staging_2 = ensure_one(sm_all.staging_id)
    assert staging_2 != staging_1

    with repo:
        pr01.post_comment('hansen retry', config['role_reviewer']['token'])
    env.run_crons()
    # retry should have re-triggered cancel-staging
    assert not staging_2.active
    assert p_01.staging_id.active

    # make the staging fail again
    with repo:
        repo.post_status('staging.master', 'failure')
    env.run_crons()

    assert not p_01.staging_id.active
    assert p_01.state == 'error'
    staging_3 = ensure_one(sm_all.staging_id)
    assert staging_3 != staging_2

    # check that updating the PR resets it to ~ready
    with repo:
        repo.make_commits(
            'heads/master',
            Commit("urgent+", tree={'y': 'es'}),
            ref="heads/Urgent1",
        )
    env.run_crons()
    assert not staging_3.active
    assert p_01.state == 'ready'
    assert p_01.priority == 'alone'
    assert p_01.skipchecks == True
    assert p_01.staging_id.active

    # r- should unstage, re-enable the checks and switch off staging
    # cancellation, but leave the priority
    with repo:
        pr01.post_comment("hansen r-", config['role_reviewer']['token'])
    env.run_crons()

    staging_4 = ensure_one(sm_all.staging_id)
    assert staging_4 != staging_3

    assert not p_01.staging_id.active
    assert p_01.state == 'opened'
    assert p_01.priority == 'alone'
    assert p_01.skipchecks == False
    assert p_01.cancel_staging == True

    assert staging_4.active, "staging should not be disabled"

    # cause the PR to become ready the normal way
    with repo:
        pr01.post_comment("hansen r+", config['role_reviewer']['token'])
        repo.post_status(p_01.head, 'success')
    env.run_crons()

    # a cancel_staging pr becoming ready should have cancelled the staging,
    # and because the PR is `alone` it should... have been restaged alone,
    # without the ready non-alone PRs
    assert not sm_all.staging_id.active
    assert p_01.staging_id.active
    assert p_01.state == 'ready'
    assert p_01.priority == 'alone'
    assert p_01.skipchecks == False
    assert p_01.cancel_staging == True

@pytest.mark.usefixtures("reviewer_admin")
def test_batching_urgenter_than_split(env, repo, config):
    """ p=alone PRs should take priority over split stagings (processing
    of a staging having CI-failed and being split into sub-stagings)
    """
    with repo:
        repo.make_commits(None, Commit('initial', tree={'a': 'some content'}), ref="heads/master")

        pr1 = _pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        pr2 = _pr(repo, 'PR2', [{'a': 'some content', 'c': 'CCC'}, {'d': 'DDD'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
    env.run_crons()

    p_1 = to_pr(env, pr1)
    p_2 = to_pr(env, pr2)
    st = env['runbot_merge.stagings'].search([])

    # both prs should be part of the staging
    assert st.mapped('batch_ids.prs') == p_1 | p_2

    # add CI failure
    with repo:
        repo.post_status('staging.master', 'failure')
    env.run_crons()

    # should have staged the first half
    assert p_1.staging_id.heads
    assert not p_2.staging_id.heads

    # during restaging of pr1, create urgent PR
    with repo:
        pr0 = _pr(repo, 'urgent', [{'a': 'a', 'b': 'b'}], user=config['role_user']['token'], reviewer=None, statuses=[])
        pr0.post_comment('hansen NOW!', config['role_reviewer']['token'])
    env.run_crons()

    assert not p_1.staging_id
    assert to_pr(env, pr0).staging_id

@pytest.mark.usefixtures("reviewer_admin")
def test_urgent_failed(env, repo, config):
    """ Ensure pr[p=0,state=failed] don't get picked up
    """
    with repo:
        repo.make_commits(None, Commit('initial', tree={'a': 'some content'}), ref='heads/master')

        pr21 = _pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])

    p_21 = to_pr(env, pr21)

    # no statuses run on PR0s
    with repo:
        pr01 = _pr(repo, 'Urgent1', [{'n': 'n'}, {'o': 'o'}], user=config['role_user']['token'], reviewer=None, statuses=[])
        pr01.post_comment('hansen NOW!', config['role_reviewer']['token'])
    p_01 = to_pr(env, pr01)
    p_01.error = True

    env.run_crons()
    assert not p_01.staging_id, "p_01 should not be picked up as it's failed"
    assert p_21.staging_id, "p_21 should have been staged"

def test_urgent_split(env, repo, config):
    """Ensure that urgent (alone) PRs which get split don't get
    double-merged
    """
    with repo:
        repo.make_commits(
            None,
            Commit("initial", tree={'a': '1'}),
            ref="heads/master"
        )

        pr01 = _pr(
            repo, "PR1", [{'b': '1'}],
            user=config['role_user']['token'],
            reviewer=None,
        )
        pr01.post_comment('hansen alone r+', config['role_reviewer']['token'])
        pr02 = _pr(
            repo, "PR2", [{'c': '1'}],
            user=config['role_user']['token'],
            reviewer=None,
        )
        pr02.post_comment('hansen alone r+', config['role_reviewer']['token'])
    env.run_crons(None)
    pr01_id = to_pr(env, pr01)
    assert pr01_id.blocked is False
    pr02_id = to_pr(env, pr02)
    assert pr01_id.blocked is False

    env.run_crons()
    st = pr01_id.staging_id
    assert st and pr02_id.staging_id == st
    with repo:
        repo.post_status('staging.master', 'failure')
    env.run_crons()
    # should have cancelled the staging, split it, and re-staged the first
    # half of the split
    assert st.state == 'failure'
    assert pr01_id.staging_id and pr01_id.staging_id != st
    assert not pr02_id.staging_id
    split_prs = env['runbot_merge.split'].search([]).batch_ids.prs
    assert split_prs == pr02_id, \
        f"only the unstaged PR {pr02_id} should be in a split, found {split_prs}"

@pytest.mark.skip(reason="Maybe nothing to do, the PR is just skipped and put in error?")
def test_batching_merge_failure(self):
    pass

def test_staging_ci_failure_batch(env, repo, config):
    """ on failure split batch & requeue
    """
    with repo:
        repo.make_commits(None, Commit('initial', tree={'a': 'some content'}), ref='heads/master')

        pr1 = _pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        pr2 = _pr(repo, 'PR2', [{'a': 'some content', 'c': 'CCC'}, {'d': 'DDD'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
    env.run_crons()

    st = env['runbot_merge.stagings'].search([])
    # both prs should be part of the staging
    assert len(st.mapped('batch_ids.prs')) == 2
    # add CI failure
    with repo:
        repo.post_status('staging.master', 'failure')

    pr1 = to_pr(env, pr1)
    pr2 = to_pr(env, pr2)

    env.run_crons()
    # should have split the existing batch into two, with one of the
    # splits having been immediately restaged
    st = env['runbot_merge.stagings'].search([])
    assert len(st) == 1
    assert pr1.staging_id and pr1.staging_id == st

    sp = env['runbot_merge.split'].search([])
    assert len(sp) == 1

    # This is the failing PR!
    with repo:
        repo.post_status('staging.master', 'failure')
    env.run_crons()
    assert pr1.state == 'error'

    assert pr2.staging_id

    with repo:
        repo.post_status('staging.master', 'success')
    env.run_crons(None)
    assert pr2.state == 'merged'


def test_presplit(env, project, repo, users, config):
    """If presplitting is enabled for the branch, the mergebot should
    immediately create two sub-stagings (1 and 2) with the relevant
    PR stacks integrated.
    """
    project.branch_ids.presplit = True
    with repo:
        repo.make_commits(None, Commit("initial", tree={"a": "a"}), ref="heads/master")

        _pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        _pr(repo, 'PR2', [{'c': 'CCC'}, {'d': 'DDD'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
    env.run_crons()

    with repo:
        repo.post_status('staging.master', 'failure')
        tree1 = repo.commit('staging.master.1').tree
        tree2 = repo.commit('staging.master.2').tree
    env.run_crons()

    staging = env['runbot_merge.stagings'].search([])
    assert staging.id == 2
    assert repo.commit(staging.head_ids.sha).tree == tree1
    with repo:
        repo.post_status('staging.master', 'failure')
    env.run_crons()

    staging = env['runbot_merge.stagings'].search([])
    assert staging.id == 3
    assert repo.commit(staging.head_ids.sha).tree == tree2


def test_prestage(env, project, repo, users, config):
    """If prestaging is enabled, and the threshold is passed, the mergebot
    should immediately create a sub-staging (3) for the still-available PRs
    """
    project.staging_pattern = "%(target)s%(sub)s-%(stage)s"
    project.staging_sub_pattern = '-%(sub)s'
    project.batch_limit = 1
    project.branch_ids.optimistic_staging_threshold = 1
    with repo:
        repo.make_commits(None, Commit("initial", tree={"a": "a"}), ref="heads/master")

        _pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        _pr(repo, 'PR2', [{'c': 'CCC'}, {'d': 'DDD'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
    env.run_crons()

    with repo:
        repo.post_status('master-staging', 'success')
        next_tree = repo.commit('master-3-staging').tree
    env.run_crons()

    staging = env['runbot_merge.stagings'].search([])
    assert staging.id == 2
    assert repo.commit(staging.head_ids.sha).tree == next_tree


def test_not_prestage(env, project, repo, users, config):
    project.batch_limit = 1
    project.branch_ids.optimistic_staging_threshold = 2
    with repo:
        repo.make_commits(None, Commit("initial", tree={"a": "a"}), ref="heads/master")

        _pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        _pr(repo, 'PR2', [{'c': 'CCC'}, {'d': 'DDD'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
    env.run_crons()

    with pytest.raises(AssertionError) as e:
        repo.commit('staging.master.3')
    # pytest assertion rewriting tacks the extra info to the user-provided
    # assertion message, so we need to strip it
    payload, _ = e.value.args[0].split('\n', 1)
    assert json.loads(payload)['status'] == '422'

def test_split_depthfirst(env, project, repo, users, config):
    project.branch_ids.depth_first_splits = True
    with repo:
        repo.make_commits(None, Commit('initial', tree={'a': 'a'}), ref='heads/master')

        prs = [
            _pr(repo, c, [{c: '1'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
            for c in "bcde"
        ]
    env.run_crons()

    pr_ids = env['runbot_merge.pull_requests'].browse(
        to_pr(env, p).id
        for p in prs
    )

    assert all(p.staging_id for p in pr_ids)
    with repo:
        repo.post_status('staging.master', 'failure')
    env.run_crons()

    assert all(p.staging_id for p in pr_ids[:2])
    assert all(not p.staging_id for p in pr_ids[2:])

    with repo:
        repo.post_status('staging.master', 'failure')
    env.run_crons()

    # should have re-staged the first PR, not the second child
    assert pr_ids[0].staging_id, [p.staging_id for p in pr_ids]
    assert all(not p.staging_id for p in pr_ids[1:])

    with repo:
        repo.post_status('staging.master', 'success')
    env.run_crons()

    assert pr_ids[0].state == 'merged'
    assert pr_ids[1].staging_id

@pytest.mark.parametrize('on_fail,cutoff', [
    # 1 is error, 2, 3, 4 are joined back into a split, 5, 6 are left out
    ('join', -2),
    # 1 is error, 2, 3, 4 and 5 are picked by new staging, 6 is left out
    ('delete', -1)
])
def test_solve_case(env, project, repo, users, config, on_fail, cutoff):
    project.batch_limit = 4
    # fail handlers make more sense using depth-first splits
    project.branch_ids.depth_first_splits = True
    project.branch_ids.on_fail = on_fail
    with repo:
        repo.make_commits(None, Commit('initial', tree={'a': 'a'}), ref='heads/master')

        prs = [
            _pr(repo, c, [{c: '1'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
            for c in "bcdefg"
        ]
    env.run_crons()

    pr_ids = env['runbot_merge.pull_requests'].browse(
        to_pr(env, p).id
        for p in prs
    )

    # fail toplevel (4) -> 2 -> 1
    for _ in range(3):
        with repo:
            repo.post_status('staging.master', 'failure')
        env.run_crons()
    # at this point, PR 1 should be marked as error, and PRs 2-(4~5) should have
    # been picked up, with PR 5+~6 being overflow
    assert pr_ids[0].error
    assert all([p.staging_id for p in pr_ids[1:cutoff]])
    assert all([not p.staging_id for p in pr_ids[cutoff:]])

def test_join_last(env, project, repo, users, config):
    project.branch_ids.on_fail = 'join'
    with repo:
        repo.make_commits(None, Commit('x', tree={'a': 'a'}), ref='heads/master')
        pr = _pr(repo, 'y', [{'c': '1'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
    env.run_crons()

    with repo:
        repo.post_status('staging.master', 'failure')
    env.run_crons()

    assert to_pr(env, pr).error