import random
import re
import shutil
import time
from operator import itemgetter

import pytest

from utils import make_basic, Commit, validate_all, matches, seen, REF_PATTERN, to_pr


def test_conflict(env, config, make_repo, users):
    """ Create a PR to A which will (eventually) conflict with C when
    forward-ported.
    """
    prod, _other = make_basic(env, config, make_repo, statuses='default')
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
        prod.post_status(p_0, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success')
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
        'h': matches('''<<<\x3c<<< $$
a
||||||| $$
=======
xxx
>>>\x3e>>> $$
'''),
    }
    assert prc.comments == [
        seen(env, prc, users),
        (users['user'],
f'''@{users['user']} @{users['reviewer']} cherrypicking of pull request {pra_id.display_name} failed.

stdout:
```
Auto-merging h
CONFLICT (add/add): Merge conflict in h

```

Either perform the forward-port manually (and push to this branch, proceeding as usual) or close this PR (maybe?).

:shipit: you can use [`git-fw`](https://raw.githubusercontent.com/odoo/runbot/refs/heads/17.0/runbot_merge/scripts/git-fw) to re-do the forward-port for you locally.

:warning: after resolving this conflict, you will need to merge it via @{project.github_prefix}.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
''')
    ]

    prb = prod.get_pr(prb_id.number)
    assert prb.comments == [
        seen(env, prb, users),
        (users['user'], '''\
This PR targets b and is part of the forward-port chain. Further PRs will be created up to d.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
'''),
        (users['user'], """@%s @%s the next pull request (%s) is in conflict. \
You can merge the chain up to here by saying
> @hansen r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""" % (
            users['user'], users['reviewer'],
            prc_id.display_name,
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
            prod.commit('c').id,
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
        prod.post_status(prc.head, 'success')
        prc.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    assert prc_id.staging_id
    with prod:
        prod.post_status('staging.c', 'success')
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

def test_conflict_unknown_root_head(
    env, config, make_repo,
):
    """Very specific implementation detail: on conflict we try to infer
    modify/delete conflicts in order to mark the files git reintrodued, to do so
    we try to list the files modified by the original PR, and that requires
    having the head of its target branch locally...
    """
    fw_cron = env.ref('runbot_merge.port_forward')
    fw_cron.active = False
    prod, _other = make_basic(env, config, make_repo, statuses="default")

    with prod:
        # generate conflict: remove f from b, while updating f in a
        prod.make_commits(
            'b', Commit('33', tree={'g': 'c'}, reset=True),
            ref='heads/b'
        )
        [p_0] = prod.make_commits(
            'a', Commit('p_0', tree={'f': 'xxx'}),
            ref='heads/a_pr'
        )
        pr = prod.make_pr(target='a', head='a_pr')
        prod.post_status(p_0, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    # there should be no forward port (yet)
    assert env['runbot_merge.pull_requests'].search_count([]) == 1
    # there should be an fw job
    assert env['forwardport.batches'].search_count([]) == 1

    # add a commit to a which the mergebot should not know about
    with prod:
        prod.make_commits(
            'a',
            Commit('X', tree={'g': 'fkdshf'}),
            ref='heads/a',
        )

    fw_cron.active = True
    fw_cron.trigger()
    env.run_crons()

    assert env['runbot_merge.pull_requests'].search_count([]) == 2

def test_massive_conflict(env, config, make_repo):
    """If the conflict is large enough, the commit message may exceed ARG_MAX
     and trigger E2BIG.
    """
    # CONFLICT (modify/delete): <file> deleted in <commit> (<title>) and modified in HEAD.  Version HEAD of <file> left in tree.
    #
    # 107 + 2 * len(filename) + len(title) per conflicting file.
    # - filename: random.randbytes(10).hex() -> 20
    # - title: random.randbytes(20).hex() -> 40
    # -> 701 (!) files

    files = [random.randbytes(10).hex() for _ in range(1500)]

    # region setup
    project = env['runbot_merge.project'].create({
        'name': "thing",
        'github_token': config['github']['token'],
        'github_prefix': 'hansen',
        'github_name': config['github']['name'],
        'github_email': "foo@example.org",
        'fp_github_token': config['github']['token'],
        'fp_github_name': 'herbert',
        'branch_ids': [
            (0, 0, {'name': 'a', 'sequence': 100}),
            (0, 0, {'name': 'b', 'sequence': 80}),
        ],
    })

    repo = make_repo("repo")
    env['runbot_merge.events_sources'].create({'repository': repo.name})

    repo_id = env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'required_statuses': "default",
        'fp_remote_target': repo.name,
        'group_id': False,
    })
    env['res.partner'].search([
        ('github_login', '=', config['role_reviewer']['user'])
    ]).write({
        'review_rights': [(0, 0, {'repository_id': repo_id.id, 'review': True})]
    })

    with repo:
        # create branch with a ton of empty files
        repo.make_commits(
            None,
            Commit(
                random.randbytes(20).hex(),
                tree=dict.fromkeys(files, "xoxo"),
            ),
            ref='heads/a',
        )

        # removes all those files in the next branch
        repo.make_commits(
            'a',
            Commit(
                random.randbytes(20).hex(),
                tree=dict.fromkeys(files, "content!"),
            ),
            ref='heads/b',
        )
    # endregion setup

    with repo:
        # update all the files
        repo.make_commits(
            'a',
            Commit(random.randbytes(20).hex(), tree={'x': '1'}, reset=True),
            ref='heads/change',
        )
        pr = repo.make_pr(target='a', head='change')
        repo.post_status('refs/heads/change', 'success')
        pr.post_comment('hansen rebase-ff r+', config['role_reviewer']['token'])
    env.run_crons()

    with repo:
        repo.post_status('staging.a', 'success')
    env.run_crons()

    # we don't actually need more, the bug crashes the forward port entirely so
    # the PR is never even created
    _pra_id, _prb_id = env['runbot_merge.pull_requests'].search([], order='number')


def test_conflict_deleted(env, config, make_repo):
    prod, _other = make_basic(env, config, make_repo, statuses="default")
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
        prod.post_status(p_0, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success')
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
    assert prod.read_tree(prod.commit(pr1.head)) == {
        'f': matches("""\
<<<\x3c<<< $$
||||||| $$
=======
xxx
>>>\x3e>>> $$
"""),
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

def test_conflict_deleted_binary(env, config, make_repo):
    """A modify/deleted conflict on a binary file should result in normal
    git behaviour of having the deleted file "reappear", as there are no
    conflict markers for binary files.
    """
    prod, _other = make_basic(env, config, make_repo, statuses="default")
    # remove f from b
    with prod:
        prod.make_commits(
            'b', Commit('33', tree={'g': 'c'}, reset=True),
            ref='heads/b'
        )

    # generate a conflict: update f in a
    with prod:
        [p_0] = prod.make_commits(
            'a', Commit('p_0', tree={'f': b'\xc0\xc1'}),
            ref='heads/conflicting'
        )
        pr = prod.make_pr(target='a', head='conflicting')
        prod.post_status(p_0, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success')
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
    assert prod.read_tree(prod.commit(pr1.head)) == {
        'f': b'\xc0\xc1',
        'g': 'c',
    }

def test_conflict_deleted_deep(env, config, make_repo):
    """ Same as the previous one but files are deeper than toplevel, and we only
    want to see if the conflict post-processing works.
    """
    # region: setup
    prod = make_repo("test")
    env['runbot_merge.events_sources'].create({'repository': prod.name})
    with prod:
        [a, b] = prod.make_commits(
            None,
            Commit("a", tree={
                "foo/bar/baz": "1",
                "foo/bar/qux": "1",
                "corge/grault": "1",
            }),
            Commit("b", tree={"foo/bar/qux": "2"}, reset=True),
        )
        prod.make_ref("heads/a", a)
        prod.make_ref("heads/b", b)

    assert prod.read_tree(prod.commit('b')).keys() == {"foo"}
    assert prod.read_tree(prod.commit('b'), recursive=True) == {
        "foo/bar/qux": "2",
    }

    project = env['runbot_merge.project'].create({
        'name': "test",
        'github_token': config['github']['token'],
        'github_prefix': 'hansen',
        'github_name': config['github']['name'],
        'github_email': "foo@example.org",
        'fp_github_token': config['github']['token'],
        'fp_github_name': 'herbert',
        'branch_ids': [
            (0, 0, {'name': 'a', 'sequence': 100}),
            (0, 0, {'name': 'b', 'sequence': 80}),
        ],
        "repo_ids": [
            (0, 0, {
                'name': prod.name,
                'required_statuses': "default",
                'fp_remote_target': prod.fork().name,
                'group_id': False,
            })
        ]
    })
    env['res.partner'].search([
        ('github_login', '=', config['role_reviewer']['user'])
    ]).write({
        'review_rights': [(0, 0, {'repository_id': project.repo_ids.id, 'review': True})]
    })
    # endregion

    with prod:
        prod.make_commits(
            'a',
            Commit("c", tree={
                "foo/bar/baz": "2",
                "corge/grault": "insert funny number",
            }),
            ref="heads/conflicting",
        )
        pr = prod.make_pr(target='a', head='conflicting')
        prod.post_status("conflicting", "success")
        pr.post_comment("hansen r+", config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()
    _, pr1 = env['runbot_merge.pull_requests'].search([], order='number')
    assert prod.read_tree(prod.commit(pr1.head), recursive=True) == {
        "foo/bar/qux": "2",
        "foo/bar/baz": """\
<<<<<<< HEAD
||||||| MERGE BASE
=======
2
>>>>>>> FORWARD PORTED
""",
        "corge/grault": """\
<<<<<<< HEAD
||||||| MERGE BASE
=======
insert funny number
>>>>>>> FORWARD PORTED
"""
    }, "the conflicting file should have had conflict markers fixed in"

def test_multiple_commits_same_authorship(env, config, make_repo):
    """ When a PR has multiple commits by the same author and its
    forward-porting triggers a conflict, the resulting (squashed) conflict
    commit should have the original author (same with the committer).
    """
    author = {'name': 'George Pearce', 'email': 'gp@example.org'}
    committer = {'name': 'G. P. W. Meredith', 'email': 'gpwm@example.org'}
    prod, _ = make_basic(env, config, make_repo, statuses='default')
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
        prod.post_status('conflicting', 'success')
        pr.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])
    env.run_crons()

    pr_id = to_pr(env, pr)
    assert pr_id.state == 'ready'
    assert pr_id.staging_id

    with prod:
        prod.post_status('staging.a', 'success')
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


def test_multiple_commits_different_authorship(env, config, make_repo, users):
    """ When a PR has multiple commits by different authors, the resulting
    (squashed) conflict commit should have an empty email
    """
    author = {'name': 'George Pearce', 'email': 'gp@example.org'}
    committer = {'name': 'G. P. W. Meredith', 'email': 'gpwm@example.org'}
    prod, _ = make_basic(env, config, make_repo, statuses='default')
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
        prod.post_status('conflicting', 'success')
        pr.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])
    env.run_crons()

    pr_id = to_pr(env, pr)
    assert pr_id.state == 'ready'
    assert pr_id.staging_id

    with prod:
        prod.post_status('staging.a', 'success')
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

    assert prod.read_tree(c)['g'] == matches('''<<<\x3c<<< $$
b
||||||| $$
=======
2
>>>\x3e>>> $$
''')

    # I'd like to fix the conflict so everything is clean and proper *but*
    # github's API apparently rejects creating commits with an empty email.
    #
    # So fuck that, I'll just "merge the conflict". Still works at simulating
    # a resolution error as technically that's the sort of things people do.

    pr2 = prod.get_pr(pr2_id.number)
    with prod:
        prod.post_status(pr2_id.head, 'success')
        pr2.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    assert pr2.comments == [
        seen(env, pr2, users),
        (users['user'], matches('@%(user)s @%(reviewer)s $$CONFLICT' % users)),
        (users['reviewer'], 'hansen r+'),
        (users['user'], f"@{users['user']} @{users['reviewer']} unable to stage: "
                        "All commits must have author and committer email, "
                        f"missing email on {pr2_id.head} indicates the "
                        "authorship is most likely incorrect."),
    ]
    assert pr2_id.state == 'error'
    assert not pr2_id.staging_id, "staging should have been rejected"

@pytest.mark.skipif(not shutil.which('mergiraf'), reason="mergiraf not available")
def test_structured_conflict_no_mergiraf(env, config, make_repo, users):
    """Check that even if mergiraf is available it will only apply when
    enabled (?)
    """
    _project, repo, _pr = setup_unstructured_conflict(env, config, make_repo)
    env.run_crons()

    with repo:
        repo.post_status('staging.a', 'success')
    env.run_crons()

    _pra_id, prb_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert repo.read_tree(repo.commit(prb_id.head)) == {
        'f.py': matches('''\
def foo():
<<<\x3c<<< $$
    a = 5
    return a
||||||| $$
    a = 0
    return a
=======
    somevar = 0
    return somevar
>>>\x3e>>> $$
'''),
    }

@pytest.mark.skipif(not shutil.which('mergiraf'), reason="mergiraf not available")
def test_structured_conflict_mergiraf(env, config, make_repo, users):
    """Check that if a conflict is not resolvable textually but is resolvable
    structurally mergiraf handles it correctly (if available).
    """
    project, repo, _pr = setup_unstructured_conflict(env, config, make_repo)
    project.use_mergiraf = True
    env.run_crons()

    with repo:
        repo.post_status('staging.a', 'success')
    env.run_crons()

    _pra_id, prb_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert repo.read_tree(repo.commit(prb_id.head)) == {
        'f.py': '''\
def foo():
    somevar = 5
    return somevar
''',
    }

def setup_unstructured_conflict(env, config, make_repo):
    project = env['runbot_merge.project'].create({
        'name': "test",
        'github_token': config['github']['token'],
        'github_prefix': 'hansen',
        'github_name': config['github']['name'],
        'github_email': "foo@example.org",
        'fp_github_token': config['github']['token'],
        'fp_github_name': 'herbert',
        'branch_ids': [
            (0, 0, {'name': 'a', 'sequence': 100}),
            (0, 0, {'name': 'b', 'sequence': 80}),
        ],
    })
    prod = make_repo("test")
    env['runbot_merge.events_sources'].create({'repository': prod.name})
    with prod:
        [a, b] = prod.make_commits(
            None,
            Commit("a", tree={'f.py': """\
def foo():
    a = 0
    return a
"""}),
            Commit("b", tree={'f.py': """\
def foo():
    a = 5
    return a
"""}),
        )
        prod.make_ref("heads/a", a)
        prod.make_ref("heads/b", b)
    other = prod.fork()
    repo = env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': prod.name,
        'required_statuses': "default",
        'fp_remote_target': other.name,
        'group_id': False,
    })
    env['res.partner'].search([
        ('github_login', '=', config['role_reviewer']['user'])
    ]).write({
        'review_rights': [(0, 0, {'repository_id': repo.id, 'review': True})]
    })

    with prod:
        prod.make_commits('a', Commit('change', tree={
            'f.py': """\
def foo():
    somevar = 0
    return somevar
"""
        }), ref="heads/mypr")
        pr = prod.make_pr(target='a', head='mypr')
        prod.post_status('mypr', 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])

    return (project, prod, pr)
