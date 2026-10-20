import re
from datetime import datetime, timedelta

import pytest

from utils import make_basic, Commit, to_pr, REF_PATTERN, seen, matches


def test_base(env, config, make_repo, users, page, botname):
    prod, other = make_basic(env, config, make_repo, statuses='default')

    b_head = prod.commit('b')
    c_head = prod.commit('c')
    with prod:
        # create PR as a user with no access to prod (or other)
        prod.make_commits(
            'a',
            Commit('p_0', tree={'x': '1'}),
            ref='heads/hugechange'
        )
        pr0 = prod.make_pr(target='a', title="super important change", head='hugechange')
        pr0.post_comment('hansen fw=skipmerge', config['role_reviewer']['token'])
    pr0_id = to_pr(env, pr0)
    assert not pr0_id.merge_date, \
        "PR obviously shouldn't have a merge date before being merged"
    assert pr0_id.batch_id.fw_policy == 'skipmerge'

    env.run_crons()

    pr0_id, pr1_id, pr2_id = env['runbot_merge.pull_requests'].search([], order='number')
    pr2_id.reminder_next = datetime.now() - timedelta(days=1)
    env.run_crons('runbot_merge.reminder')

    assert pr0_id.number == pr0.number
    assert pr0.comments == [
        (users['reviewer'], 'hansen fw=skipmerge'),
        (botname, seen(env, pr0)),
        (botname, "Starting forward-port. Not waiting for merge to create followup forward-ports."),
    ]

    assert pr1_id.parent_id == pr0_id
    assert pr1_id.source_id == pr0_id
    other_owner = other.name.split('/')[0]
    assert re.match(other_owner + ':' + REF_PATTERN.format(target='b', source='hugechange'), pr1_id.label), \
        "check that FP PR was created in FP target repo"
    assert pr1_id.ping == f"@{users['user']} ", "not reviewed yet so ping should only include author"
    assert prod.read_tree(prod.commit(pr1_id.head)) == {
        'f': 'c',
        'g': 'b',
        'x': '1'
    }
    pr1 = prod.get_pr(pr1_id.number)
    assert pr1.comments == [
        (botname, seen(env, pr1)),
        (botname, """\
This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""")]

    assert pr2_id.parent_id == pr1_id
    assert pr2_id.source_id == pr0_id
    assert re.match(REF_PATTERN.format(target='c', source='hugechange'), pr2_id.refname), \
        "check that FP PR was created in FP target repo"
    assert prod.read_tree(prod.commit(pr2_id.head)) == {
        'f': 'c',
        'g': 'a',
        'h': 'a',
        'x': '1'
    }
    pr2 = prod.get_pr(pr2_id.number)
    assert pr2.comments == [
        (botname, seen(env, pr2)),
        (botname, """\
@%s this PR targets c and is the last of the forward-port chain containing:
* %s

To merge the full chain, use
> @hansen r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""" % (
            users['user'],
            pr1_id.display_name,
    )),
        (botname, "@%s this forward port of %s is awaiting action (not merged or closed)." % (
            users['user'],
            pr0_id.display_name,
        ))
    ]

    with prod:
        prod.post_status(pr0_id.head, 'success')
        prod.post_status(pr1_id.head, 'success')
        prod.post_status(pr2_id.head, 'success')

        pr2.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()

    assert pr0_id.staging_id
    assert pr1_id.staging_id
    assert pr2_id.staging_id
    # three branches so should have three stagings
    assert len(pr0_id.staging_id | pr1_id.staging_id | pr2_id.staging_id) == 3
    # validate
    with prod:
        prod.post_status('staging.a', 'success')
        prod.post_status('staging.b', 'success')
        prod.post_status('staging.c', 'success')

    # and trigger merge
    env.run_crons()
    assert all(p.state == 'merged' for p in [pr0_id, pr1_id, pr2_id])

    head_a = prod.commit('a')
    assert head_a.message == f"""\
p_0

closes {pr0_id.display_name}

Signed-off-by: {pr0_id.reviewed_by.formatted_email}\
"""

    old_b = prod.read_tree(b_head)
    head_b = prod.commit('b')
    assert head_b.message == f"""\
p_0

closes {pr1_id.display_name}

X-original-commit: {pr0_id.head}
Signed-off-by: {pr1_id.reviewed_by.formatted_email}\
""", "since the previous PR is not merged we don't know what its final commit is (?)"
    b_tree = prod.read_tree(head_b)
    assert b_tree == {**old_b, 'x': '1'}

    old_c = prod.read_tree(c_head)
    head_c = prod.commit('c')
    assert head_c.message == f"""\
p_0

closes {pr2_id.display_name}

X-original-commit: {pr0_id.head}
Signed-off-by: {pr2_id.reviewed_by.formatted_email}\
"""
    c_tree = prod.read_tree(head_c)
    assert c_tree == {**old_c, 'x': '1'}

    # check that we didn't just smash the original trees
    assert prod.read_tree(prod.commit('a')) != b_tree != c_tree

    assert pr2_id.parent_id == pr1_id
    assert pr1_id.parent_id == pr0_id

def test_conflict_recovery_source(env, config, make_repo, users, page):
    """ Source recovery is when a forward port is in conflict and we update
    *the source* in such a way that the fp doesn't conflict anymore. This should
    resume forward-porting.
    """
    prod, _ = make_basic(env, config, make_repo, statuses='default')

    with prod:
        prod.make_commits("b", Commit("xxx", tree={'x': '1'}), ref='heads/b')
        prod.make_commits("c", Commit("xxx", tree={'x': '2'}), ref='heads/c')
        prod.make_commits(
            'a',
            Commit('p_0', tree={'x': '0'}),
            ref='heads/hugechange'
        )
        pr0 = prod.make_pr(target='a', title="super important change", head='hugechange')
        pr0.post_comment('hansen fw=skipmerge', config['role_reviewer']['token'])
    env.run_crons()

    pr0_id, pr1_id, pr2_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert prod.read_tree(prod.commit(pr1_id.head)) == {
        'f': 'c',
        'g': 'b',
        'x': matches('''\
<<<\x3c<<< $$
1
||||||| $$
=======
0
>>>\x3e>>> $$
''')
    }
    assert prod.read_tree(prod.commit(pr2_id.head)) == {
        'f': 'c',
        'g': 'a',
        'h': 'a',
        'x': matches('''\
<<<\x3c<<< $$
2
||||||| $$
=======
0
>>>\x3e>>> $$
''')
    }
    assert pr1_id.parent_id and pr2_id.parent_id

    pr1 = prod.get_pr(pr1_id.number)
    assert "CONFLICT (add/add)" in pr1.comments[1][1]

    # Oh no, conflict! But turns out we can implement the fix differently <smort>
    with prod:
        prod.make_commits("a", Commit("workaround", tree={"y": "1"}), ref="heads/hugechange", make=False)
    assert prod.read_tree(prod.commit(pr0_id.head)) == {
        'f': 'e',
        'y': '1',
    }

    env.run_crons()

    assert env['runbot_merge.pull_requests'].search_count([]) == 3,\
        "check that we have not created separate new versions of the prs"
    assert prod.read_tree(prod.commit(pr1_id.head)) == {
        'f': 'c',
        'g': 'b',
        'x': '1',
        'y': '1',
    }
    assert prod.read_tree(prod.commit(pr2_id.head)) == {
        'f': 'c',
        'g': 'a',
        'h': 'a',
        'x': '2',
        'y': '1',
    }

def test_conflict_recovery_manual(env, config, make_repo, users, page):
    """ Manual recover is when a forward port is in conflict and we update
    *that forward port*. This should resume forward porting when skipmerge.
    """
    prod, _ = make_basic(env, config, make_repo, statuses='default')

    with prod:
        prod.make_commits("b", Commit("xxx", tree={'x': '1'}), ref='heads/b')
        prod.make_commits("c", Commit("xxx", tree={'x': '1'}), ref='heads/c')
        prod.make_commits(
            'a',
            Commit('p_0', tree={'x': '0'}),
            ref='heads/hugechange'
        )
        pr0 = prod.make_pr(target='a', title="super important change", head='hugechange')
        pr0.post_comment('hansen fw=skipmerge', config['role_reviewer']['token'])
    env.run_crons()

    _pr0_id, pr1_id, pr2_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert prod.read_tree(prod.commit(pr1_id.head)) == {
        'f': 'c',
        'g': 'b',
        'x': matches('''\
<<<\x3c<<< $$
1
||||||| $$
=======
0
>>>\x3e>>> $$
''')
    }
    assert prod.read_tree(prod.commit(pr2_id.head)) == {
        'f': 'c',
        'g': 'a',
        'h': 'a',
        'x': matches('''\
<<<\x3c<<< $$
1
||||||| $$
=======
0
>>>\x3e>>> $$
''')
    }
    assert pr1_id.parent_id and pr2_id.parent_id, "conflicts on skipmerge should not detach"

    pr1 = prod.get_pr(pr1_id.number)
    assert "CONFLICT (add/add)" in pr1.comments[1][1]
    assert "CONFLICT (add/add)" in prod.get_pr(pr2_id.number).comments[1][1]


    prb_repo, prb_ref = pr1.branch
    with prb_repo:
        prb_repo.make_commits(
            prod.commit("b").id,
            Commit("thing", tree={'x': 'yyy'}),
            ref=f'heads/{prb_ref}',
            make=False,
        )
    assert not pr1_id.parent_id, "manual update should still de-parent even on skipmerge"
    pr1_head = pr1_id.head
    assert prod.read_tree(prod.commit(pr1_head)) == {
        'f': 'c',
        'g': 'b',
        'x': 'yyy',
    }
    env.run_crons()
    assert env['runbot_merge.pull_requests'].search_count([]) == 3,\
        "check that we have not created a separate new version of pr3"

    assert prod.read_tree(prod.commit(pr2_id.head)) == {
        'f': 'c',
        'g': 'a',
        'h': 'a',
        'x': 'yyy',
    }


    with prod:
        prod.make_commits(
            'a',
            Commit('p_0', tree={'x': '42'}),
            ref='heads/hugechange'
        )
    env.run_crons()
    assert pr1.head == pr1_head, "since PR1 is detached the update to pr0 should not propagate"

def test_suppress_ping_on_conflict(env, config, make_repo, users, botname):
    """The default ping-on-conflict is useful to notify the author that they
    will need an action to resume forward porting.

    However in the case of skipmerge, conflicts are pretty likely, and may be
    multitude. As such a recap of conflicts *could* be useful but a conflict
    notification for each one is a bit much.
    """
    r1, _ = make_basic(env, config, make_repo, statuses='default')
    r2, _ = make_basic(env, config, make_repo, statuses='default')
    with r1, r2:
        r1.make_commits("a", Commit("c", tree={'x': '0'}), ref="heads/hugechange")
        # setup conflict for forward port
        r1.make_commits("b", Commit("conflict", tree={'x': '1'}), ref="heads/b")
        pr1_1 = r1.make_pr(target='a', title="super important change", head='hugechange')
        r2.make_commits("a", Commit("c", tree={'x': '0'}), ref="heads/hugechange")
        pr2_1 = r2.make_pr(target='a', title="super important change", head='hugechange')
        pr2_1.post_comment('hansen fw=skipmerge', config['role_reviewer']['token'])
    env.run_crons()

    assert env['runbot_merge.pull_requests'].search_count([]) == 6
    pr1_1_id = to_pr(env, pr1_1)
    pr1_2_id = env['runbot_merge.pull_requests'].search([
        ('source_id', '=', pr1_1_id.id),
        ('target.name', '=', 'b'),
    ])
    pr1_2 = r1.get_pr(pr1_2_id.number)
    project = env['runbot_merge.project'].search([])
    assert pr1_2.comments == [
        (botname, seen(env, pr1_2)),
        (botname, f"""\
cherrypicking of pull request {pr1_1_id.display_name} failed.

stdout:
```
Auto-merging x
CONFLICT (add/add): Merge conflict in x

```

Either perform the forward-port manually (and push to this branch, proceeding as usual) or close this PR (maybe?).

:shipit: you can use [`git-fw`](https://raw.githubusercontent.com/odoo/runbot/refs/heads/17.0/runbot_merge/scripts/git-fw) to re-do the forward-port for you locally.

:warning: after resolving this conflict, you will need to merge it via @{project.github_prefix}.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
"""),
    ]

    pr2_1_id = to_pr(env, pr2_1)
    pr2_2_id = env['runbot_merge.pull_requests'].search([
        ('source_id', '=', pr2_1_id.id),
        ('target.name', '=', 'b'),
    ])
    pr2_2 = r2.get_pr(pr2_2_id.number)
    assert pr2_2.comments == [
        (botname, seen(env, pr2_2)),
        (botname, f"""\
while this was properly forward-ported, at least one co-dependent PR ({pr1_2_id.display_name}) did not succeed. You will need to fix it before this can be merged.

Both this PR and the others will need to be approved via `@{project.github_prefix} r+` as they are all considered “in conflict”.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""")
    ]


def test_suppress_ping_on_update(env, config, make_repo, users, botname):
    """By default, if a PR is updated and its parent is merged, that parent gets
    a notification. However, in the case of skipmerge such updates are more
    likely than not, leading to unnecessary amounts of notifications. We can
    probably consider that if someone needs skipmerge they'll be babying the
    thing and will keep track of its state before finally getting rid of it.
    """
    r1, _ = make_basic(env, config, make_repo, statuses='default')
    with r1:
        r1.make_commits("a", Commit("c", tree={'x': '0'}), ref="heads/hugechange")
        pr1 = r1.make_pr(target='a', title="super important change", head='hugechange')
        pr1.post_comment('hansen fw=skipmerge', config['role_reviewer']['token'])
    env.run_crons()

    assert env['runbot_merge.pull_requests'].search_count([]) == 3
    _pr1_id, pr2_id, _pr3_id = env['runbot_merge.pull_requests'].search([], order='number')

    pr2 = r1.get_pr(pr2_id.number)
    pr_repo, pr2_ref = pr2.branch
    oldhead = pr2.head
    with pr_repo:
        [c] = pr_repo.make_commits(
            r1.commit("b").id,
            Commit("cc", tree={'x': '1'}),
            ref=f'heads/{pr2_ref}',
            make=False,
        )
    env.run_crons()

    assert pr2.comments == [
        (botname, seen(env, pr2)),
        (botname, "This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.\n\nMore info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port\n"),
    ]
    assert pr1.comments == [
        (users['reviewer'], "hansen fw=skipmerge"),
        (botname, seen(env, pr1)),
        (botname, "Starting forward-port. Not waiting for merge to create followup forward-ports."),
        (botname, f"child PR {pr2_id.display_name} has become a normal PR because head updated from {oldhead} to {c}. This PR (and any of its parents) will need to be merged independently as approvals won't cross."),
    ]


def test_forwardport_resume_skipmerge(env, config, make_repo, users, botname):
    prod, _ = make_basic(env, config, make_repo, statuses='default')

    with prod:
        prod.make_commits("b", Commit("up", tree={'x': '1'}), ref='heads/b')
        prod.make_commits("c", Commit("up", tree={'x': '1'}), ref='heads/c')

        prod.make_commits("a", Commit("initial", tree={'x': '0'}), ref='heads/abranch')
        pr0 = prod.make_pr(target='a', title="super important change", head='abranch')
        prod.post_status('abranch', 'success')
        pr0.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    pr0_id = to_pr(env, pr0)
    assert pr0_id.state == 'merged'

    pr1_id = env['runbot_merge.pull_requests'].search([('source_id', '=', pr0_id.id), ('target.name', '=', 'b')])
    pr1 = prod.get_pr(pr1_id.number)
    repo, ref = pr1.branch

    assert repo.read_tree(repo.commit(pr1_id.head)) == {
        'f': 'c',
        'g': 'b',
        'x': matches('''\
<<<\x3c<<< $$
1
||||||| $$
=======
0
>>>\x3e>>> $$
'''),
    }

    assert env['runbot_merge.pull_requests'].search_count([]) == 2, "check that no forward port happened"
    with prod, repo:
        repo.make_commits(prod.commit("b").id, Commit("fix conflict", tree={'x': '01'}), ref=f'heads/{ref}', make=False)
        pr1.post_comment('hansen fw=skipmerge', config['role_reviewer']['token'])
    env.run_crons()
    assert pr1_id.state == 'opened'
    assert repo.read_tree(repo.commit(pr1_id.head)) == {
        'f': 'c',
        'g': 'b',
        'x': '01',
    }

    pr2_id = env['runbot_merge.pull_requests'].search([('source_id', '=', pr0_id.id), ('target.name', '=', 'c')])
    assert pr2_id, "skipmerge caused a forward port to be forced even with no statuses"

    assert repo.read_tree(repo.commit(pr2_id.head)) == {
        'f': 'c',
        'g': 'a',
        'h': 'a',
        'x': '01',
    }

    project = env['runbot_merge.project'].search([])
    assert pr1.comments == [
        (botname, seen(env, pr1)),
        (botname, f"""\
@{users['user']} @{users['reviewer']} cherrypicking of pull request {pr0_id.display_name} failed.

stdout:
```
Auto-merging x
CONFLICT (add/add): Merge conflict in x

```

Either perform the forward-port manually (and push to this branch, proceeding as usual) or close this PR (maybe?).

:shipit: you can use [`git-fw`](https://raw.githubusercontent.com/odoo/runbot/refs/heads/17.0/runbot_merge/scripts/git-fw) to re-do the forward-port for you locally.

:warning: after resolving this conflict, you will need to merge it via @{project.github_prefix}.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
"""),
        (users['reviewer'], "hansen fw=skipmerge"),
        (botname, "Forcing all forward ports."),
    ]

    pr2 = prod.get_pr(pr2_id.number)
    assert pr2.comments == [
        (botname, seen(env, pr2)),
        (botname, f"""\
@{users['user']} @{users['reviewer']} this PR targets c and is the last of the forward-port chain.

To merge the full chain, use
> @{project.github_prefix} r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""")
    ]
