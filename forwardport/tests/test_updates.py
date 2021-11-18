"""
Test cases for updating PRs during after the forward-porting process after the
initial merge has succeeded (and forward-porting has started)
"""
import re
import sys

import pytest

from utils import seen, re_matches, Commit, make_basic, to_pr


def test_update_pr(env, config, make_repo, users):
    """ Even for successful cherrypicks, it's possible that e.g. CI doesn't
    pass or the reviewer finds out they need to update the code.

    In this case, all following forward ports should... be detached? Or maybe
    only this one and its dependent should be updated?
    """
    prod, _ = make_basic(env, config, make_repo)
    with prod:
        [p_1] = prod.make_commits(
            'a',
            Commit('p_0', tree={'x': '0'}),
            ref='heads/hugechange'
        )
        pr = prod.make_pr(target='a', head='hugechange')
        prod.post_status(p_1, 'success', 'legal/cla')
        prod.post_status(p_1, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

    # should merge the staging then create the FP PR
    env.run_crons()

    pr0_id, pr1_id = env['runbot_merge.pull_requests'].search([], order='number')

    fp_intermediate = (users['user'], '''\
This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
''')
    ci_warning = (users['user'], 'Ping @%(user)s, @%(reviewer)s\n\nci/runbot failed on this forward-port PR' % users)

    # oh no CI of the first FP PR failed!
    # simulate status being sent multiple times (e.g. on multiple repos) with
    # some delivery lag allowing for the cron to run between each delivery
    for st, ctx in [('failure', 'ci/runbot'), ('failure', 'ci/runbot'), ('success', 'legal/cla'), ('success', 'legal/cla')]:
        with prod:
            prod.post_status(pr1_id.head, st, ctx)
        env.run_crons()
    with prod: # should be ignored because the description doesn't matter
        prod.post_status(pr1_id.head, 'failure', 'ci/runbot', description="HAHAHAHAHA")
    env.run_crons()
    # check that FP did not resume & we have a ping on the PR
    assert env['runbot_merge.pull_requests'].search([], order='number') == pr0_id | pr1_id,\
        "forward port should not continue on CI failure"
    pr1_remote = prod.get_pr(pr1_id.number)
    assert pr1_remote.comments == [seen(env, pr1_remote, users), fp_intermediate, ci_warning]

    # it was a false positive, rebuild... it fails again!
    with prod:
        prod.post_status(pr1_id.head, 'failure', 'ci/runbot', target_url='http://example.org/4567890')
    env.run_crons()
    # check that FP did not resume & we have a ping on the PR
    assert env['runbot_merge.pull_requests'].search([], order='number') == pr0_id | pr1_id,\
        "ensure it still hasn't restarted"
    assert pr1_remote.comments == [seen(env, pr1_remote, users), fp_intermediate, ci_warning, ci_warning]

    # nb: updating the head would detach the PR and not put it in the warning
    # path anymore

    # rebuild again, finally passes
    with prod:
        prod.post_status(pr1_id.head, 'success', 'ci/runbot')
    env.run_crons()

    pr0_id, pr1_id, pr2_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert pr1_id.parent_id == pr0_id
    assert pr2_id.parent_id == pr1_id
    pr1_head = pr1_id.head
    pr2_head = pr2_id.head

    # turns out branch b is syntactically but not semantically compatible! It
    # needs x to be 5!
    pr_repo, pr_ref = prod.get_pr(pr1_id.number).branch
    with pr_repo:
        # force-push correct commit to PR's branch
        [new_c] = pr_repo.make_commits(
            pr1_id.target.name,
            Commit('whop whop', tree={'x': '5'}),
            ref='heads/%s' % pr_ref,
            make=False
        )
    env.run_crons()

    assert pr1_id.head == new_c != pr1_head, "the FP PR should be updated"
    assert not pr1_id.parent_id, "the FP PR should be detached from the original"
    assert pr1_remote.comments == [
        seen(env, pr1_remote, users),
        fp_intermediate, ci_warning, ci_warning,
        (users['user'], "This PR was modified / updated and has become a normal PR. It should be merged the normal way (via @%s)" % pr1_id.repository.project_id.github_prefix),
    ], "users should be warned that the PR has become non-FP"
    # NOTE: should the followup PR wait for pr1 CI or not?
    assert pr2_id.head != pr2_head
    assert pr2_id.parent_id == pr1_id, "the followup PR should still be linked"

    assert prod.read_tree(prod.commit(pr1_id.head)) == {
        'f': 'c',
        'g': 'b',
        'x': '5'
    }, "the FP PR should have the new code"
    assert prod.read_tree(prod.commit(pr2_id.head)) == {
        'f': 'c',
        'g': 'a',
        'h': 'a',
        'x': '5'
    }, "the followup FP should also have the update"

def test_update_merged(env, make_repo, config, users):
    """ Strange things happen when an FP gets closed / merged but then its
    parent is modified and the forwardport tries to update the (now merged)
    child.

    Turns out the issue is the followup: given a PR a and forward port targets
    B -> C -> D. When a is merged we get b, c and d. If c gets merged *then*
    b gets updated, the fwbot will update c in turn, then it will look for the
    head of the updated c in order to create d.

    However it *will not* find that head, as update events don't get propagated
    on closed PRs (this is generally a good thing). As a result, the sanity
    check when trying to port c to d will fail.

    After checking with nim, the safest behaviour seems to be:

    * stop at the update of the first closed or merged PR
    * signal on that PR that something fucky happened
    * also maybe disable or exponentially backoff the update job after some
      number of attempts?
    """
    prod, _ = make_basic(env, config, make_repo)
    # add a 4th branch
    with prod:
        prod.make_ref('heads/d', prod.commit('c').id)
    env['runbot_merge.project'].search([]).write({
        'branch_ids': [(0, 0, {
            'name': 'd', 'fp_sequence': -1, 'fp_target': True,
        })]
    })

    with prod:
        [c] = prod.make_commits('a', Commit('p_0', tree={'0': '0'}), ref='heads/hugechange')
        pr = prod.make_pr(target='a', head='hugechange')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()

    _, pr1_id = env['runbot_merge.pull_requests'].search([], order='number')
    with prod:
        prod.post_status(pr1_id.head, 'success', 'legal/cla')
        prod.post_status(pr1_id.head, 'success', 'ci/runbot')
    env.run_crons()

    pr0_id, pr1_id, pr2_id = env['runbot_merge.pull_requests'].search([], order='number')
    pr2 = prod.get_pr(pr2_id.number)
    with prod:
        pr2.post_comment('hansen r+', config['role_reviewer']['token'])
        prod.post_status(pr2_id.head, 'success', 'legal/cla')
        prod.post_status(pr2_id.head, 'success', 'ci/runbot')
    env.run_crons()

    assert pr2_id.staging_id
    with prod:
        prod.post_status('staging.c', 'success', 'legal/cla')
        prod.post_status('staging.c', 'success', 'ci/runbot')
    env.run_crons()
    assert pr2_id.state == 'merged'
    assert pr2.state == 'closed'

    # now we can try updating pr1 and see what happens
    repo, ref = prod.get_pr(pr1_id.number).branch
    with repo:
        repo.make_commits(
            pr1_id.target.name,
            Commit('2', tree={'0': '0', '1': '1'}),
            ref='heads/%s' % ref,
            make=False
        )
    updates = env['forwardport.updates'].search([])
    assert updates
    assert updates.original_root == pr0_id
    assert updates.new_root == pr1_id
    env.run_crons()
    assert not pr1_id.parent_id
    assert not env['forwardport.updates'].search([])

    assert pr2.comments == [
        seen(env, pr2, users),
        (users['user'], '''This PR targets c and is part of the forward-port chain. Further PRs will be created up to d.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
'''),
        (users['reviewer'], 'hansen r+'),
        (users['user'], """Ancestor PR %s has been updated but this PR is merged and can't be updated to match.

You may want or need to manually update any followup PR.""" % pr1_id.display_name)
    ]

def test_duplicate_fw(env, make_repo, setreviewers, config, users):
    """ Test for #451
    """
    # 0 - 1 - 2 - 3 - 4  master
    #             \ - 31 v3
    #         \ - 21     v2
    #     \ - 11         v1
    repo = make_repo('proj')
    with repo:
        _, c1, c2, c3, _ = repo.make_commits(
            None,
            Commit('0', tree={'f': 'a'}),
            Commit('1', tree={'f': 'b'}),
            Commit('2', tree={'f': 'c'}),
            Commit('3', tree={'f': 'd'}),
            Commit('4', tree={'f': 'e'}),
            ref='heads/master'
        )
        repo.make_commits(c1, Commit('11', tree={'g': 'a'}), ref='heads/v1')
        repo.make_commits(c2, Commit('21', tree={'h': 'a'}), ref='heads/v2')
        repo.make_commits(c3, Commit('31', tree={'i': 'a'}), ref='heads/v3')

    proj = env['runbot_merge.project'].create({
        'name': 'a project',
        'github_token': config['github']['token'],
        'github_prefix': 'hansen',
        'fp_github_token': config['github']['token'],
        'branch_ids': [
            (0, 0, {'name': 'master', 'fp_sequence': 0, 'fp_target': True}),
            (0, 0, {'name': 'v3', 'fp_sequence': 1, 'fp_target': True}),
            (0, 0, {'name': 'v2', 'fp_sequence': 2, 'fp_target': True}),
            (0, 0, {'name': 'v1', 'fp_sequence': 3, 'fp_target': True}),
        ],
        'repo_ids': [
            (0, 0, {
                'name': repo.name,
                'required_statuses': 'ci',
                'fp_remote_target': repo.name,
            })
        ]
    })
    setreviewers(*proj.repo_ids)

    # create a PR in v1, merge it, then create all 3 ports
    with repo:
        repo.make_commits('v1', Commit('c0', tree={'z': 'a'}), ref='heads/hugechange')
        prv1 = repo.make_pr(target='v1', head='hugechange')
        repo.post_status('hugechange', 'success', 'ci')
        prv1.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    PRs = env['runbot_merge.pull_requests']
    prv1_id = PRs.search([
        ('repository.name', '=', repo.name),
        ('number', '=', prv1.number),
    ])
    assert prv1_id.state == 'ready'
    with repo:
        repo.post_status('staging.v1', 'success', 'ci')
    env.run_crons()
    assert prv1_id.state == 'merged'

    parent = prv1_id
    while True:
        child = PRs.search([('parent_id', '=', parent.id)])
        if not child:
            break

        assert child.state == 'opened'
        with repo:
            repo.post_status(child.head, 'success', 'ci')
        env.run_crons()
        parent = child
    pr_ids = _, prv2_id, prv3_id, prmaster_id = PRs.search([], order='number')
    _, prv2, prv3, prmaster = [repo.get_pr(p.number) for p in pr_ids]
    assert pr_ids.mapped('target.name') == ['v1', 'v2', 'v3', 'master']
    assert pr_ids.mapped('state') == ['merged', 'validated', 'validated', 'validated']
    assert repo.read_tree(repo.commit(prmaster_id.head)) == {'f': 'e', 'z': 'a'}

    with repo:
        repo.make_commits('v2', Commit('c0', tree={'z': 'b'}), ref=prv2.ref, make=False)
    env.run_crons()
    assert pr_ids.mapped('state') == ['merged', 'opened', 'validated', 'validated']
    assert repo.read_tree(repo.commit(prv2_id.head)) == {'f': 'c', 'h': 'a', 'z': 'b'}
    assert repo.read_tree(repo.commit(prv3_id.head)) == {'f': 'd', 'i': 'a', 'z': 'b'}
    assert repo.read_tree(repo.commit(prmaster_id.head)) == {'f': 'e', 'z': 'b'}

    assert prv2_id.source_id == prv1_id
    assert not prv2_id.parent_id

    env.run_crons()
    assert PRs.search([], order='number') == pr_ids

    with repo:
        repo.post_status(prv2.head, 'success', 'ci')
        prv2.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with repo:
        repo.post_status('staging.v2', 'success', 'ci')
    env.run_crons()
    # env.run_crons()
    assert PRs.search([], order='number') == pr_ids

def test_subsequent_conflict(env, make_repo, config, users):
    """ Test for updating an fw PR in the case where it produces a conflict in
    the followup. Cf #467.
    """
    repo, fork = make_basic(env, config, make_repo)

    # create a PR in branch A which adds a new file
    with repo:
        repo.make_commits('a', Commit('newfile', tree={'x': '0'}), ref='heads/pr1')
        pr_1 = repo.make_pr(target='a', head='pr1')
        repo.post_status('pr1', 'success', 'legal/cla')
        repo.post_status('pr1', 'success', 'ci/runbot')
        pr_1.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with repo:
        repo.post_status('staging.a', 'success', 'legal/cla')
        repo.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()
    pr1_id = to_pr(env, pr_1)
    assert pr1_id.state == 'merged'

    pr2_id = env['runbot_merge.pull_requests'].search([('source_id', '=', pr1_id.id)])
    assert pr2_id
    with repo:
        repo.post_status(pr2_id.head, 'success', 'legal/cla')
        repo.post_status(pr2_id.head, 'success', 'ci/runbot')
    env.run_crons()

    pr3_id = env['runbot_merge.pull_requests'].search([('parent_id', '=', pr2_id.id)])
    assert pr3_id
    assert repo.read_tree(repo.commit(pr3_id.head)) == {
        'f': 'c',
        'g': 'a',
        'h': 'a',
        'x': '0',
    }

    # update pr2: add a file "h"
    pr2 = repo.get_pr(pr2_id.number)
    t = {**repo.read_tree(repo.commit(pr2_id.head)), 'h': 'conflict!'}
    with fork:
        fork.make_commits(pr2_id.target.name, Commit('newfiles', tree=t), ref=pr2.ref, make=False)
    env.run_crons()

    assert repo.read_tree(repo.commit(pr3_id.head)) == {
        'f': 'c',
        'g': 'a',
        'h': re_matches(r'''<<<\x3c<<< HEAD
a
=======
conflict!
>>>\x3e>>> [0-9a-f]{7,}.*
'''),
        'x': '0',
    }
    # skip comments:
    # 1. link to mergebot status page
    # 2. "forward port chain" bit
    # 3. updated / modified & got detached
    assert pr2.comments[3:] == [
        (users['user'], f"WARNING: the latest change ({pr2_id.head}) triggered "
                        f"a conflict when updating the next forward-port "
                        f"({pr3_id.display_name}), and has been ignored.\n\n"
                        f"You will need to update this pull request "
                        f"differently, or fix the issue by hand on "
                        f"{pr3_id.display_name}.")
    ]
    # skip comments:
    # 1. link to status page
    # 2. forward-port chain thing
    assert repo.get_pr(pr3_id.number).comments[2:] == [
        (users['user'], re_matches(f'''\
WARNING: the update of {pr2_id.display_name} to {pr2_id.head} has caused a \
conflict in this pull request, data may have been lost.

stdout:
```.*?
CONFLICT \(add/add\): Merge conflict in h.*?
```

stderr:
```
\\d{{2}}:\\d{{2}}:\\d{{2}}.\\d+ .* {pr2_id.head}
error: could not apply [0-9a-f]+\\.\\.\\. newfiles
''', re.DOTALL))
    ]
