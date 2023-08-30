import re
import time
from operator import itemgetter

from utils import make_basic, Commit, validate_all, re_matches, seen, REF_PATTERN, to_pr


def test_conflict(env, config, make_repo, users):
    """ Create a PR to A which will (eventually) conflict with C when
    forward-ported.
    """
    prod, other = make_basic(env, config, make_repo)
    # create a d branch
    with prod:
        prod.make_commits('c', Commit('1111', tree={'i': 'a'}), ref='heads/d')
    project = env['runbot_merge.project'].search([])
    project.write({
        'branch_ids': [
            (0, 0, {'name': 'd', 'sequence': 40})
        ]
    })

    # generate a conflict: create a h file in a PR to a
    with prod:
        [p_0] = prod.make_commits(
            'a', Commit('p_0', tree={'h': 'xxx'}),
            ref='heads/conflicting'
        )
        pr = prod.make_pr(target='a', head='conflicting')
        prod.post_status(p_0, 'success', 'legal/cla')
        prod.post_status(p_0, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()
    pra_id, prb_id = env['runbot_merge.pull_requests'].search([], order='number')
    # mark pr b as OK so it gets ported to c
    with prod:
        validate_all([prod], [prb_id.head])
    env.run_crons()

    pra_id, prb_id, prc_id = env['runbot_merge.pull_requests'].search([], order='number')
    # should have created a new PR
    # but it should not have a parent, and there should be conflict markers
    assert not prc_id.parent_id
    assert prc_id.source_id == pra_id
    assert prc_id.state == 'opened'

    p = prod.commit(p_0)
    prc = prod.get_pr(prc_id.number)
    c = prod.commit(prc_id.head)
    assert c.author == p.author
    # ignore date as we're specifically not keeping the original's
    without_date = itemgetter('name', 'email')
    assert without_date(c.committer) == without_date(p.committer)
    assert prod.read_tree(c) == {
        'f': 'c',
        'g': 'a',
        'h': re_matches(r'''<<<\x3c<<< HEAD
a
|||||||| parent of [\da-f]{7,}.*
=======
xxx
>>>\x3e>>> [\da-f]{7,}.*
'''),
    }
    assert prc.comments == [
        seen(env, prc, users),
        (users['user'], re_matches(
fr'''@{users['user']} @{users['reviewer']} cherrypicking of pull request {pra_id.display_name} failed\.

stdout:
```
Auto-merging h
CONFLICT \(add/add\): Merge conflict in h

```

stderr:
```
.*
```

Either perform the forward-port manually \(and push to this branch, proceeding as usual\) or close this PR \(maybe\?\)\.

In the former case, you may want to edit this PR message as well\.

:warning: after resolving this conflict, you will need to merge it via @{project.github_prefix}\.

More info at https://github\.com/odoo/odoo/wiki/Mergebot#forward-port
''', re.DOTALL))
    ]
    with prod:
        prc.post_comment(f'@{project.fp_github_name} r+', config['role_reviewer']['token'])
    env.run_crons()
    assert prc_id.state == 'opened', "approving via fw should not work on a conflict"

    prb = prod.get_pr(prb_id.number)
    assert prb.comments == [
        seen(env, prb, users),
        (users['user'], '''\
This PR targets b and is part of the forward-port chain. Further PRs will be created up to d.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
'''),
        (users['user'], """@%s @%s the next pull request (%s) is in conflict. \
You can merge the chain up to here by saying
> @%s r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""" % (
            users['user'], users['reviewer'],
            prc_id.display_name,
            project.fp_github_name
        ))
    ]

    # check that CI passing does not create more PRs
    with prod:
        validate_all([prod], [prc_id.head])
    env.run_crons()
    time.sleep(5)
    env.run_crons()
    assert pra_id | prb_id | prc_id == env['runbot_merge.pull_requests'].search([], order='number'),\
        "CI passing should not have resumed the FP process on a conflicting PR"

    # fix the PR, should behave as if this were a normal PR
    prc = prod.get_pr(prc_id.number)
    pr_repo, pr_ref = prc.branch
    with pr_repo:
        pr_repo.make_commits(
            # if just given a branch name, goes and gets it from pr_repo whose
            # "b" was cloned before that branch got rolled back
            'c',
            Commit('h should indeed be xxx', tree={'h': 'xxx'}),
            ref='heads/%s' % pr_ref,
            make=False,
        )
    env.run_crons()
    assert prod.read_tree(prod.commit(prc_id.head)) == {
        'f': 'c',
        'g': 'a',
        'h': 'xxx',
    }
    assert prc_id.state == 'opened', "state should be open still"
    assert ('#%d' % pra_id.number) in prc_id.message

    # check that merging the fixed PR fixes the flow and restarts a forward
    # port process
    with prod:
        prod.post_status(prc.head, 'success', 'legal/cla')
        prod.post_status(prc.head, 'success', 'ci/runbot')
        prc.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    assert prc_id.staging_id
    with prod:
        prod.post_status('staging.c', 'success', 'legal/cla')
        prod.post_status('staging.c', 'success', 'ci/runbot')
    env.run_crons()

    *_, prd_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert ('#%d' % pra_id.number) in prd_id.message, \
        "check that source / PR A is referenced by resume PR"
    assert ('#%d' % prc_id.number) in prd_id.message, \
        "check that parent / PR C is referenced by resume PR"
    assert prd_id.parent_id == prc_id
    assert prd_id.source_id == pra_id
    assert re.match(
        REF_PATTERN.format(target='d', source='conflicting'),
        prd_id.refname
    )
    assert prod.read_tree(prod.commit(prd_id.head)) == {
        'f': 'c',
        'g': 'a',
        'h': 'xxx',
        'i': 'a',
    }

def test_conflict_deleted(env, config, make_repo):
    prod, other = make_basic(env, config, make_repo)
    # remove f from b
    with prod:
        prod.make_commits(
            'b', Commit('33', tree={'g': 'c'}, reset=True),
            ref='heads/b'
        )

    # generate a conflict: update f in a
    with prod:
        [p_0] = prod.make_commits(
            'a', Commit('p_0', tree={'f': 'xxx'}),
            ref='heads/conflicting'
        )
        pr = prod.make_pr(target='a', head='conflicting')
        prod.post_status(p_0, 'success', 'legal/cla')
        prod.post_status(p_0, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

    env.run_crons()
    # wait a bit for PR webhook... ?
    time.sleep(5)
    env.run_crons()

    # should have created a new PR
    pr0, pr1 = env['runbot_merge.pull_requests'].search([], order='number')
    # but it should not have a parent
    assert not pr1.parent_id
    assert pr1.source_id == pr0
    assert prod.read_tree(prod.commit('b')) == {
        'g': 'c',
    }
    assert pr1.state == 'opened'
    # NOTE: no actual conflict markers because pr1 essentially adds f de-novo
    assert prod.read_tree(prod.commit(pr1.head)) == {
        'f': 'xxx',
        'g': 'c',
    }

    # check that CI passing does not create more PRs
    with prod:
        validate_all([prod], [pr1.head])
    env.run_crons()
    time.sleep(5)
    env.run_crons()
    assert pr0 | pr1 == env['runbot_merge.pull_requests'].search([], order='number'),\
        "CI passing should not have resumed the FP process on a conflicting PR"

    # fix the PR, should behave as if this were a normal PR
    get_pr = prod.get_pr(pr1.number)
    pr_repo, pr_ref = get_pr.branch
    with pr_repo:
        pr_repo.make_commits(
            # if just given a branch name, goes and gets it from pr_repo whose
            # "b" was cloned before that branch got rolled back
            prod.commit('b').id,
            Commit('f should indeed be removed', tree={'g': 'c'}, reset=True),
            ref='heads/%s' % pr_ref,
            make=False,
        )
    env.run_crons()
    assert prod.read_tree(prod.commit(pr1.head)) == {
        'g': 'c',
    }
    assert pr1.state == 'opened', "state should be open still"

def test_multiple_commits_same_authorship(env, config, make_repo):
    """ When a PR has multiple commits by the same author and its
    forward-porting triggers a conflict, the resulting (squashed) conflict
    commit should have the original author (same with the committer).
    """
    author = {'name': 'George Pearce', 'email': 'gp@example.org'}
    committer = {'name': 'G. P. W. Meredith', 'email': 'gpwm@example.org'}
    prod, _ = make_basic(env, config, make_repo)
    with prod:
        # conflict: create `g` in `a`, using two commits
        prod.make_commits(
            'a',
            Commit('c0', tree={'g': '1'},
                   author={**author, 'date': '1932-10-18T12:00:00Z'},
                   committer={**committer, 'date': '1932-11-02T12:00:00Z'}),
            Commit('c1', tree={'g': '2'},
                   author={**author, 'date': '1932-11-12T12:00:00Z'},
                   committer={**committer, 'date': '1932-11-13T12:00:00Z'}),
            ref='heads/conflicting'
        )
        pr = prod.make_pr(target='a', head='conflicting')
        prod.post_status('conflicting', 'success', 'legal/cla')
        prod.post_status('conflicting', 'success', 'ci/runbot')
        pr.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])
    env.run_crons()

    pr_id = to_pr(env, pr)
    assert pr_id.state == 'ready'
    assert pr_id.staging_id

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()

    for _ in range(20):
        pr_ids = env['runbot_merge.pull_requests'].search([], order='number')
        if len(pr_ids) == 2:
            _   , pr2_id = pr_ids
            break
        time.sleep(0.5)
    else:
        assert 0, "timed out"

    c = prod.commit(pr2_id.head)
    get = itemgetter('name', 'email')
    assert get(c.author) == get(author)
    assert get(c.committer) == get(committer)

def test_multiple_commits_different_authorship(env, config, make_repo, users, rolemap):
    """ When a PR has multiple commits by different authors, the resulting
    (squashed) conflict commit should have an empty email
    """
    author = {'name': 'George Pearce', 'email': 'gp@example.org'}
    committer = {'name': 'G. P. W. Meredith', 'email': 'gpwm@example.org'}
    prod, _ = make_basic(env, config, make_repo)
    with prod:
        # conflict: create `g` in `a`, using two commits
        # just swap author and committer in the commits
        prod.make_commits(
            'a',
            Commit('c0', tree={'g': '1'},
                   author={**author, 'date': '1932-10-18T12:00:00Z'},
                   committer={**committer, 'date': '1932-11-02T12:00:00Z'}),
            Commit('c1', tree={'g': '2'},
                   author={**committer, 'date': '1932-11-12T12:00:00Z'},
                   committer={**author, 'date': '1932-11-13T12:00:00Z'}),
            ref='heads/conflicting'
        )
        pr = prod.make_pr(target='a', head='conflicting')
        prod.post_status('conflicting', 'success', 'legal/cla')
        prod.post_status('conflicting', 'success', 'ci/runbot')
        pr.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])
    env.run_crons()

    pr_id = to_pr(env, pr)
    assert pr_id.state == 'ready'
    assert pr_id.staging_id

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()

    for _ in range(20):
        pr_ids = env['runbot_merge.pull_requests'].search([], order='number')
        if len(pr_ids) == 2:
            _, pr2_id = pr_ids
            break
        time.sleep(0.5)
    else:
        assert 0, "timed out"

    c = prod.commit(pr2_id.head)
    assert len(c.parents) == 1
    get = itemgetter('name', 'email')
    bot = pr_id.repository.project_id.fp_github_name
    assert get(c.author) == (bot, ''), \
        "In a multi-author PR, the squashed conflict commit should have the " \
        "author set to the bot but an empty email"
    assert get(c.committer) == (bot, '')

    assert re.match(r'''<<<\x3c<<< HEAD
b
|||||||| parent of [\da-f]{7,}.*
=======
2
>>>\x3e>>> [\da-f]{7,}.*
''', prod.read_tree(c)['g'])

    # I'd like to fix the conflict so everything is clean and proper *but*
    # github's API apparently rejects creating commits with an empty email.
    #
    # So fuck that, I'll just "merge the conflict". Still works at simulating
    # a resolution error as technically that's the sort of things people do.

    pr2 = prod.get_pr(pr2_id.number)
    with prod:
        prod.post_status(pr2_id.head, 'success', 'legal/cla')
        prod.post_status(pr2_id.head, 'success', 'ci/runbot')
        pr2.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    assert pr2.comments == [
        seen(env, pr2, users),
        (users['user'], re_matches(r'@%s @%s .*CONFLICT' % (users['user'], users['reviewer']), re.DOTALL)),
        (users['reviewer'], 'hansen r+'),
        (users['user'], f"@{users['user']} @{users['reviewer']} unable to stage: "
                        "All commits must have author and committer email, "
                        f"missing email on {pr2_id.head} indicates the "
                        "authorship is most likely incorrect."),
    ]
    assert pr2_id.state == 'error'
    assert not pr2_id.staging_id, "staging should have been rejected"
