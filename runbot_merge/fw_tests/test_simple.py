# -*- coding: utf-8 -*-
from __future__ import annotations

import collections
import contextlib
import re
import time
import typing
from collections.abc import Iterator
from datetime import datetime, timedelta
from operator import attrgetter
from unittest import mock
from urllib.parse import quote

import pytest
import requests

from utils import seen, Commit, make_basic as make_basic_utils, REF_PATTERN, MESSAGE_TEMPLATE, validate_all, part_of, to_pr, matches, Repo

FMT = '%Y-%m-%d %H:%M:%S'
FAKE_PREV_WEEK = (datetime.now() + timedelta(days=1)).strftime(FMT)

@pytest.fixture(params=["statuses", "runbot"])
def make_basic(
    request,
    port,
    env,
    config,
    RepoType,
    make_repo,
) -> Iterator[MakeBasic]:
    match request.param:
        case "statuses":
            staging_statuses = True
            cm = contextlib.nullcontext()
        case "runbot":
            staging_statuses = False
            def _post_status(repo, ref, status, context='default', **kw):
                if m := re.search(project.staging_pattern % {
                    'stage': 'staging',
                    'target': '(.*?)',
                    'sub': '',
                } + '$', ref):
                    st = env['runbot_merge.stagings'].search([('target.name', '=', m[1])])
                    oid = st.id
                else:
                    if re.fullmatch(r'[a-z0-9]{40}', ref, flags=re.IGNORECASE):
                        cond = ('head', '=', ref)
                    else:
                        cond = ('label', '=like', '%:' + ref.removeprefix('heads/'))
                    for i in range(5):
                        pr = env['runbot_merge.pull_requests'].search([
                            ('repository.name', '=', repo.name),
                            cond
                        ])
                        if pr:
                            oid = quote(pr.display_name.replace('#', '/'))
                            break
                        time.sleep(i)
                    else:
                        raise TimeoutError(f"could not find PR {cond }")

                sha = repo.commit(ref).id
                r = requests.post(
                    f"http://localhost:{port}/runbot_merge/{oid}/statuses",
                    data={'sha': sha, 'context': context, 'status': status, **kw},
                )
                assert r.ok, r.reason
            # !!! this needs to be the concrete type not the protocol !!!
            cm = mock.patch.object(RepoType, "post_status", _post_status)
        case _:
            raise ValueError(f"Unknown staging mode {request.param!r}")

    project_name = "someproject"
    project = env['runbot_merge.project'].create({
        'name': project_name,
        'github_token': config['github']['token'],
        'github_prefix': 'hansen',
        'github_name': config['github']['name'],
        'github_email': "foo@example.org",
        'fp_github_token': config['github']['token'],
        'fp_github_name': 'herbert',
        'branch_ids': [
            (0, 0, {'name': 'a', 'sequence': 100}),
            (0, 0, {'name': 'b', 'sequence': 80}),
            (0, 0, {'name': 'c', 'sequence': 60}),
        ],
        'staging_statuses': staging_statuses,
    })

    with cm:
        yield lambda name="proj": make_basic_utils(
            env,
            config,
            make_repo,
            project_name=project_name,
            reponame=name,
            statuses='default',
        )

class MakeBasic(typing.Protocol):
    def __call__(self, name: str = "proj") -> tuple[Repo, Repo]: ...

# need:
# * an odoo server
#   - connected to a database
#   - with relevant modules loaded / installed
#   - set up project
#   - add repo, branch(es)
#   - provide visibility to contents si it can be queried & al
# * a tunnel so the server is visible from the outside (webhooks)
# * the ability to create repos on github
#   - repo name
#   - a github user to create a repo with
#   - a github owner to create a repo *for*
#   - provide ability to create commits, branches, prs, ...
@pytest.mark.parametrize('staging_pattern', [
    "%(stage)s.%(target)s%(sub)s",
    "%(target)s%(sub)s-%(stage)s",
])
def test_straightforward_flow(env, config, users, make_basic, staging_pattern):
    # TODO: ~all relevant data in users when creating partners
    # get reviewer's name
    reviewer_name = env['res.partner'].search([
        ('github_login', '=', users['reviewer'])
    ]).name

    prod, other = make_basic()
    env['runbot_merge.project'].search([]).write({
        'staging_pattern': staging_pattern,
    })
    other_user = config['role_other']
    other_user_repo = prod.fork(token=other_user['token'])

    b_head = prod.commit('b')
    c_head = prod.commit('c')
    with prod, other_user_repo:
        # create PR as a user with no access to prod (or other)
        [_, p_1] = other_user_repo.make_commits(
            prod.commit('a').id,
            Commit('p_0', tree={'x': '0'}),
            Commit('p_1', tree={'x': '1'}),
            ref='heads/hugechange'
        )
        pr = prod.make_pr(
            target='a', title="super important change",
            head=other_user['user'] + ':hugechange',
            token=other_user['token']
        )
        prod.post_status(p_1, 'success')
        # use rebase-ff (instead of rebase-merge) so we don't have to dig in
        # parents of the merge commit to find the cherrypicks
        pr.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])
    pr_id = to_pr(env, pr)
    assert not pr_id.merge_date,\
        "PR obviously shouldn't have a merge date before being merged"

    env.run_crons()
    with prod:
        prod.post_status(staging_pattern % {
            'stage': 'staging',
            'target': 'a',
            'sub': ''
        }, 'success')

    # should merge the staging then create the FP PR
    env.run_crons()

    assert datetime.now() - datetime.strptime(pr_id.merge_date, FMT) <= timedelta(minutes=1),\
        "check if merge date was set about now (within a minute as crons and " \
        "RPC calls yield various delays before we're back)"

    p_1_merged = prod.commit('a')

    assert p_1_merged.id != p_1
    assert p_1_merged.message == MESSAGE_TEMPLATE.format(
        message='p_1',
        repo=prod.name,
        number=pr.number,
        headers='',
        name=reviewer_name,
        email=config['role_reviewer']['email'],
    )
    assert prod.read_tree(p_1_merged) == {
        'f': 'e',
        'x': '1',
    }, "ensure p_1_merged has ~ the same contents as p_1 but is a different commit"
    [p_0_merged] = p_1_merged.parents

    # wait a bit for PR webhook... ?
    time.sleep(5)
    env.run_crons()

    pr0, pr1 = env['runbot_merge.pull_requests'].search([], order='number')
    assert pr0.number == pr.number
    # 50 lines in, we can start checking the forward port...
    assert pr1.parent_id == pr0
    assert pr1.source_id == pr0
    other_owner = other.name.split('/')[0]
    assert re.match(other_owner + ':' + REF_PATTERN.format(target='b', source='hugechange'), pr1.label), \
        "check that FP PR was created in FP target repo"
    c = prod.commit(pr1.head)
    # TODO: add original committer (if !author) as co-author in commit message?
    assert c.author['name'] == other_user['user'], "author should still be original's probably"
    assert c.committer['name'] == other_user['user'], "committer should also still be the original's, really"

    assert pr1.ping == "@%s @%s " % (
        config['role_other']['user'],
        config['role_reviewer']['user'],
    ), "ping of forward-port PR should include author and reviewer of source"

    assert prod.read_tree(c) == {
        'f': 'c',
        'g': 'b',
        'x': '1'
    }
    with prod:
        prod.post_status(pr1.head, 'success')

    env.run_crons()
    pr0_, pr1_, pr2 = env['runbot_merge.pull_requests'].search([], order='number')
    pr2.reminder_next = datetime.now() - timedelta(days=1)
    env.run_crons('runbot_merge.reminder')


    assert pr.comments == [
        (users['reviewer'], 'hansen r+ rebase-ff'),
        seen(env, pr, users),
        (users['user'], 'Merge method set to rebase and fast-forward.'),
    ]
    pr1_remote = prod.get_pr(pr1.number)
    assert pr1_remote.comments == [
        seen(env, pr1_remote, users),
        (users['user'], """\
This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""")]

    assert pr0_ == pr0
    assert pr1_ == pr1
    assert pr1.parent_id == pr1.source_id == pr0
    assert pr2.parent_id == pr1
    assert pr2.source_id == pr0
    assert not pr0.squash, "original PR has >1 commit"
    assert not (pr1.squash or pr2.squash), "forward ports should also have >1 commit"
    assert re.match(REF_PATTERN.format(target='c', source='hugechange'), pr2.refname), \
        "check that FP PR was created in FP target repo"
    assert prod.read_tree(prod.commit(pr2.head)) == {
        'f': 'c',
        'g': 'a',
        'h': 'a',
        'x': '1'
    }
    pr2_remote = prod.get_pr(pr2.number)
    assert pr2_remote.comments == [
        seen(env, pr2_remote, users),
        (users['user'], """\
@%s @%s this PR targets c and is the last of the forward-port chain containing:
* %s

To merge the full chain, use
> @hansen r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""" % (
            users['other'], users['reviewer'],
            pr1.display_name,
    )),
        (users['user'], "@%s @%s this forward port of %s is awaiting action (not merged or closed)." % (
            users['other'],
            users['reviewer'],
            pr0.display_name,
        ))
    ]
    with prod:
        prod.post_status(pr2.head, 'success')

        pr2_remote.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()

    assert pr1.staging_id
    assert pr2.staging_id
    # two branches so should have two stagings
    assert pr1.staging_id != pr2.staging_id
    # validate
    with prod:
        prod.post_status(staging_pattern % {
            'stage': 'staging',
            'target': 'b',
            'sub': ''
        }, 'success')
        prod.post_status(staging_pattern % {
            'stage': 'staging',
            'target': 'c',
            'sub': ''
        }, 'success')

    # and trigger merge
    env.run_crons()

    # apparently github strips out trailing newlines when fetching through the
    # API...
    message_template = MESSAGE_TEMPLATE.format(
        message='p_1',
        repo=prod.name,
        number='%s',
        headers=f'X-original-commit: {p_1_merged.id}\n',
        name=reviewer_name,
        email=config['role_reviewer']['email'],
    )

    old_b = prod.read_tree(b_head)
    head_b = prod.commit('b')
    assert head_b.message == message_template % pr1.number
    assert prod.commit(head_b.parents[0]).message == part_of(f'p_0\n\nX-original-commit: {p_0_merged}', pr1, separator='\n')
    b_tree = prod.read_tree(head_b)
    assert b_tree == {
        **old_b,
        'x': '1',
    }
    old_c = prod.read_tree(c_head)
    head_c = prod.commit('c')
    assert head_c.message == message_template % pr2.number
    assert prod.commit(head_c.parents[0]).message == part_of(f'p_0\n\nX-original-commit: {p_0_merged}', pr2, separator='\n')
    c_tree = prod.read_tree(head_c)
    assert c_tree == {
        **old_c,
        'x': '1',
    }
    # check that we didn't just smash the original trees
    assert prod.read_tree(prod.commit('a')) != b_tree != c_tree

    prs = env['forwardport.branch_remover'].search([]).mapped('pr_id')
    assert prs == pr0 | pr1 | pr2, "pr1 and pr2 should be slated for branch deletion"
    env.run_crons('runbot_merge.remover', context={'forwardport_merged_before': FAKE_PREV_WEEK})

    # should not have deleted the base branch (wrong repo)
    assert other_user_repo.get_ref(pr.ref) == p_1

    # should have deleted all PR branches
    pr1_ref = pr1_remote.ref
    with pytest.raises(AssertionError, match='Not Found'):
        other.get_ref(pr1_ref)

    pr2_ref = pr2_remote.ref
    with pytest.raises(AssertionError, match="Not Found"):
        other.get_ref(pr2_ref)

def test_empty(env, config, users, make_basic):
    """ Cherrypick of an already cherrypicked (or separately implemented)
    commit -> conflicting pr.
    """
    prod, _other = make_basic()
    # merge change to b
    with prod:
        [p_0] = prod.make_commits(
            'b', Commit('p', tree={'x': '0'}),
            ref='heads/early'
        )
        pr0 = prod.make_pr(target='b', head='early')
        prod.post_status(p_0, 'success')
        pr0.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.b', 'success')

    # merge same change to a afterwards
    with prod:
        [p_1] = prod.make_commits(
            'a', Commit('p_0', tree={'x': '0'}),
            ref='heads/late'
        )
        pr1 = prod.make_pr(target='a', head='late')
        prod.post_status(p_1, 'success')
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    assert prod.read_tree(prod.commit('a')) == {
        'f': 'e',
        'x': '0',
    }
    assert prod.read_tree(prod.commit('b')) == {
        'f': 'c',
        'g': 'b',
        'x': '0',
    }
    # should have 4 PRs:
    # PR 0
    # FP of PR 0 to C
    # PR 1
    # failed FP of PR1 to B
    prs = env['runbot_merge.pull_requests'].search([], order='number')
    assert len(prs) == 4
    pr0_id = prs.filtered(lambda p: p.number == pr0.number)
    pr1_id = prs.filtered(lambda p: p.number == pr1.number)
    fp_id = prs.filtered(lambda p: p.parent_id == pr0_id)
    fail_id = prs - (pr0_id | pr1_id | fp_id)
    assert fp_id
    assert fail_id
    # unlinked from parent since cherrypick failed
    assert not fail_id.parent_id
    # the tree should be clean...
    assert prod.read_tree(prod.commit(fail_id.head)) == {
        'f': 'c',
        'g': 'b',
        'x': '0',
    }

    with prod:
        prod.post_status(fp_id.head, 'success')
        prod.post_status(fail_id.head, 'success')
    env.run_crons()

    # should not have created any new PR
    assert env['runbot_merge.pull_requests'].search([], order='number') == prs
    # change FP token to see if the feedback comes from the proper user
    project = env['runbot_merge.project'].search([])
    project.write({
        'fp_github_name': False,
        'fp_github_token': config['role_other']['token'],
    })
    assert project.fp_github_name == users['other']

    # check reminder
    fail_id.reminder_next = datetime.now() - timedelta(days=1)
    env.run_crons('runbot_merge.reminder')
    fail_id.reminder_next = datetime.now() - timedelta(days=1)
    env.run_crons('runbot_merge.reminder')

    awaiting = (
        users['other'],
        '@%s @%s this forward port of %s is awaiting action (not merged or closed).' % (
            users['user'], users['reviewer'],
            pr1_id.display_name
        )
    )
    conflict = (users['user'], matches(
        f"""@{users['user']} @{users['reviewer']} cherrypicking of pull request {pr1_id.display_name} failed.

stdout:
```
$$
```

stderr:
```
$$
```

Either perform the forward-port manually (and push to this branch, proceeding as usual) or close this PR (maybe?).

:shipit: you can use [`git-fw`](https://raw.githubusercontent.com/odoo/runbot/refs/heads/17.0/runbot_merge/scripts/git-fw) to re-do the forward-port for you locally.

:warning: after resolving this conflict, you will need to merge it via @{project.github_prefix}.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
"""))
    assert pr1.comments == [
        (users['reviewer'], 'hansen r+'),
        seen(env, pr1, users),
    ]
    fail_pr = prod.get_pr(fail_id.number)
    assert fail_pr.comments == [
        seen(env, fail_pr, users),
        conflict,
        awaiting,
        awaiting,
    ], "each cron run should trigger a new message"
    # check that this stops if we close the PR
    with prod:
        fail_pr.close()
    fail_id.reminder_next = datetime.now() - timedelta(days=1)
    env.run_crons('runbot_merge.reminder')
    assert pr1.comments == [
        (users['reviewer'], 'hansen r+'),
        seen(env, pr1, users),
    ]
    assert fail_pr.comments[2:] == [awaiting]*2,\
        "message should not be triggered on closed PR"

    with prod:
        fail_pr.open()
    with prod:
        prod.post_status(fail_id.head, 'success')
    assert fail_id.state == 'validated'
    fail_id.reminder_next = datetime.now() - timedelta(days=1)
    env.run_crons('runbot_merge.reminder')
    assert fail_pr.comments[2:] == [awaiting]*3, "check that message triggers again"

    with prod:
        fail_pr.post_comment('hansen r+', config['role_reviewer']['token'])
    assert fail_id.state == 'ready'
    fail_id.reminder_next = datetime.now() - timedelta(days=1)
    env.run_crons('runbot_merge.reminder')
    assert fail_pr.comments[2:] == [
        awaiting,
        awaiting,
        awaiting,
        (users['reviewer'], "hansen r+"),
    ],"if a PR is ready (unblocked), the reminder should not trigger as "\
        "we're likely waiting for the mergebot"

def test_partially_empty(env, config, make_basic):
    """ Check what happens when only some commits of the PR are now empty
    """
    prod, _other = make_basic()
    # merge change to b
    with prod:
        [p_0] = prod.make_commits(
            'b', Commit('p', tree={'x': '0'}),
            ref='heads/early'
        )
        pr0 = prod.make_pr(target='b', head='early')
        prod.post_status(p_0, 'success')
        pr0.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.b', 'success')

    # merge same change to a afterwards
    with prod:
        [*_, p_1] = prod.make_commits(
            'a',
            Commit('p_0', tree={'w': '0'}),
            Commit('p_1', tree={'x': '0'}),
            Commit('p_2', tree={'y': '0'}),
            ref='heads/late'
        )
        pr1 = prod.make_pr(target='a', head='late')
        prod.post_status(p_1, 'success')
        pr1.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success')

    env.run_crons()
    assert prod.read_tree(prod.commit('a')) == {
        'f': 'e',
        'w': '0',
        'x': '0',
        'y': '0',
    }
    assert prod.read_tree(prod.commit('b')) == {
        'f': 'c',
        'g': 'b',
        'x': '0',
    }

    fail_id = env['runbot_merge.pull_requests'].search([
        ('number', 'not in', [pr0.number, pr1.number]),
        ('parent_id', '=', False),
    ], order='number')
    assert fail_id
    # unlinked from parent since cherrypick failed
    assert not fail_id.parent_id
    # the tree should be clean...
    assert prod.read_tree(prod.commit(fail_id.head)) == {
        'f': 'c',
        'g': 'b',
        'w': '0',
        'x': '0',
        'y': '0',
    }

Case = collections.namedtuple('Case', 'author reviewer delegate success')
ACL = [
    Case('reviewer', 'reviewer', None, True),
    Case('reviewer', 'self_reviewer', None, False),
    Case('reviewer', 'other', None, False),
    Case('reviewer', 'other', 'other', True),

    Case('self_reviewer', 'reviewer', None, True),
    Case('self_reviewer', 'self_reviewer', None, True),
    Case('self_reviewer', 'other', None, False),
    Case('self_reviewer', 'other', 'other', True),

    Case('other', 'reviewer', None, True),
    Case('other', 'self_reviewer', None, False),
    Case('other', 'other', None, True),
    Case('other', 'other', 'other', True),
]
@pytest.mark.parametrize(Case._fields, ACL)
def test_access_rights(env, config, users, author, reviewer, delegate, success, make_basic):
    """Validates the review rights *for the forward-port sequence*, the original
    PR is always reviewed by `user`.
    """
    prod, _other = make_basic()
    project = env['runbot_merge.project'].search([])

    # create a partner for `user`
    c = env['res.partner'].create({
        'name': users['user'],
        'github_login': users['user'],
        'email': 'user@example.org',
    })
    c.write({
        'review_rights': [
            (0, 0, {'repository_id': repo.id, 'review': True})
            for repo in project.repo_ids
        ]
    })
    # create a partner for `other` so we can put an email on it
    env['res.partner'].create({
        'name': users['other'],
        'github_login': users['other'],
        'email': 'other@example.org',
    })

    author_token = config['role_' + author]['token']
    fork = prod.fork(token=author_token)
    with prod, fork:
        [c] = fork.make_commits(prod.commit('a').id, Commit('c_0', tree={'y': '0'}), ref='heads/accessrights')
        pr = prod.make_pr(
            target='a', title='my change',
            head=users[author] + ':accessrights',
            token=author_token,
        )
        prod.post_status(c, 'success')
        pr.post_comment('hansen r+', token=config['github']['token'])
        if delegate:
            pr.post_comment('hansen delegate=%s' % users[delegate], token=config['github']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    pr0, pr1 = env['runbot_merge.pull_requests'].search([], order='number')
    assert pr0.state == 'merged'
    with prod:
        prod.post_status(pr1.head, 'success')
    env.run_crons()

    _, _, pr2 = env['runbot_merge.pull_requests'].search([], order='number')
    with prod:
        prod.post_status(pr2.head, 'success')
        prod.get_pr(pr2.number).post_comment(
            'hansen r+',
            token=config['role_' + reviewer]['token']
        )
    env.run_crons()
    if success:
        assert pr1.staging_id and pr2.staging_id,\
            "%s should have approved FP of PRs by %s" % (reviewer, author)
        st = prod.commit('staging.b')
        # Should be signed-off by both original reviewer and forward port reviewer
        original_signoff = signoff(config['role_user'], st.message)
        forward_signoff = signoff(config['role_' + reviewer], st.message)
        assert st.message.index(original_signoff) <= st.message.index(forward_signoff),\
            "Check that FP approver is after original PR approver as that's " \
            "the review path for the PR"
    else:
        assert not (pr1.staging_id or pr2.staging_id),\
            "%s should *not* have approved FP of PRs by %s" % (reviewer, author)
def signoff(conf, message):
    for n in filter(None, [conf.get('name'), conf.get('user')]):
        signoff = 'Signed-off-by: ' + n
        if signoff in message:
            return signoff
    raise AssertionError("Failed to find signoff by %s in %s" % (conf, message))

def test_disapproval(env, config, users, make_basic):
    """The author of a source PR should be able to unapprove the forward port in
    case they approved it then noticed an issue of something.
    """
    # region setup
    prod, _ = make_basic()
    env['res.partner'].create({
        'name': users['other'],
        'github_login': users['other'],
        'email': 'other@example.org',
    })

    author_token = config['role_other']['token']
    fork = prod.fork(token=author_token)
    with prod, fork:
        [c] = fork.make_commits(prod.commit('a').id, Commit('c_0', tree={'y': '0'}), ref='heads/accessrights')
        pr0 = prod.make_pr(
            target='a', title='my change',
            head=users['other'] + ':accessrights',
            token=author_token,
        )
        prod.post_status(c, 'success')
        pr0.post_comment('hansen r+', token=config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    pr0_id, pr1_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert pr1_id.source_id == pr0_id
    pr1 = prod.get_pr(pr1_id.number)
    assert pr0_id.state == 'merged'
    with prod:
        prod.post_status(pr1_id.head, 'success')
    env.run_crons()
    # endregion

    _, _, pr2_id = env['runbot_merge.pull_requests'].search([], order='number')
    pr2 = prod.get_pr(pr2_id.number)
    with prod:
        prod.post_status(pr2_id.head, 'success')
        pr2.post_comment('hansen r+', token=config['role_other']['token'])
    # no point creating staging for our needs, just propagate statuses
    env.run_crons(None)
    assert pr1_id.state == 'ready'
    assert pr2_id.state == 'ready'

    # oh no, pr1 has an error!
    with prod:
        pr1.post_comment('hansen r-', token=config['role_other']['token'])
    env.run_crons(None)
    assert pr1_id.state == 'validated', "pr1 should not be approved anymore"
    assert pr2_id.state == 'ready', "pr2 should not be affected"

    assert pr1.comments == [
        seen(env, pr1, users),
        (users['user'], 'This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.\n\nMore info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port\n'),
        (users['other'], "hansen r-"),
        (users['user'], "Note that only this forward-port has been unapproved, "
                        "sibling forward ports may have to be unapproved "
                        "individually."),
    ]
    with prod:
        pr2.post_comment('hansen r-', token=config['role_other']['token'])
    env.run_crons(None)
    assert pr2_id.state == 'validated'
    assert pr2.comments[2:] == [
        (users['other'], "hansen r+"),
        (users['other'], "hansen r-"),
    ], "shouldn't have a warning on r- because it's the only approved PR of the chain"

def test_delegate_fw(env, config, users, make_basic):
    """If a user is delegated *on a forward port* they should be able to approve
    *the followup*.
    """
    prod, _ = make_basic()
    # create a partner for `other` so we can put an email on it
    env['res.partner'].create({
        'name': users['other'],
        'github_login': users['other'],
        'email': 'other@example.org',
    })
    author_token = config['role_self_reviewer']['token']
    fork = prod.fork(token=author_token)
    with prod, fork:
        [c] = fork.make_commits(prod.commit('a').id, Commit('c_0', tree={'y': '0'}), ref='heads/accessrights')
        pr = prod.make_pr(
            target='a', title='my change',
            head=users['self_reviewer'] + ':accessrights',
            token=author_token,
        )
        prod.post_status(c, 'success')
        pr.post_comment('hansen r+', token=config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    # ensure pr1 has to be approved to be forward-ported
    _, pr1_id = env['runbot_merge.pull_requests'].search([], order='number')
    # detatch from source
    pr1_id.write({
        'parent_id': False,
        'detach_reason': "Detached for testing.",
    })
    with prod:
        prod.post_status(pr1_id.head, 'success')
    env.run_crons()
    pr1 = prod.get_pr(pr1_id.number)
    # delegate review to "other" consider PR fixed, and have "other" approve it
    with prod:
        pr1.post_comment('hansen delegate=' + users['other'],
                         token=config['role_reviewer']['token'])
        prod.post_status(pr1_id.head, 'success')
        pr1.post_comment('hansen r+', token=config['role_other']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.b', 'success')
    env.run_crons()

    _, _, pr2_id = env['runbot_merge.pull_requests'].search([], order='number')
    pr2 = prod.get_pr(pr2_id.number)
    # make "other" also approve this one
    with prod:
        prod.post_status(pr2_id.head, 'success')
        pr2.post_comment('hansen r+', token=config['role_other']['token'])
    env.run_crons()

    assert pr2.comments == [
        seen(env, pr2, users),
        (users['user'], '''@{self_reviewer} @{reviewer} this PR targets c and is the last of the forward-port chain.

To merge the full chain, use
> @hansen r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
'''.format_map(users)),
        (users['other'], 'hansen r+')
    ]
    assert pr2_id.reviewed_by


def test_redundant_approval(env, config, users, make_basic):
    """If a forward port sequence has been partially approved, fw-bot r+ should
    not perform redundant approval as that triggers warning messages.
    """
    prod, _ = make_basic()
    with prod:
        prod.make_commits(
            'a', Commit('p', tree={'x': '0'}),
            ref='heads/early'
        )
        pr0 = prod.make_pr(target='a', head='early')
        prod.post_status('heads/early', 'success')
        pr0.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()
    pr0_id, pr1_id = env['runbot_merge.pull_requests'].search([], order='number asc')
    with prod:
        prod.post_status(pr1_id.head, 'success')
    env.run_crons()

    _, _, pr2_id = env['runbot_merge.pull_requests'].search([], order='number asc')
    assert pr2_id.parent_id == pr1_id
    assert pr1_id.parent_id == pr0_id

    pr1 = prod.get_pr(pr1_id.number)
    pr2 = prod.get_pr(pr2_id.number)
    with prod:
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])
    with prod:
        pr2.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    assert pr1.comments == [
        seen(env, pr1, users),
        (users['user'], 'This PR targets b and is part of the forward-port chain. '
                        'Further PRs will be created up to c.\n\n'
                        'More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port\n'),
        (users['reviewer'], 'hansen r+'),
    ]


def test_batched(env, config, users, make_basic):
    """ Tests for projects with multiple repos & sync'd branches. Batches
    should be FP'd to batches
    """
    main1, _ = make_basic('main1')
    main2, _ = make_basic('main2')
    main1.unsubscribe(config['role_reviewer']['token'])
    main2.unsubscribe(config['role_reviewer']['token'])

    friendo = config['role_other']
    other1 = main1.fork(token=friendo['token'])
    other2 = main2.fork(token=friendo['token'])

    with main1, other1:
        [c1] = other1.make_commits(
            main1.commit('a').id, Commit('commit repo 1', tree={'1': 'a'}),
            ref='heads/contribution'
        )
        pr1 = main1.make_pr(
            target='a', title="My contribution",
            head=friendo['user'] + ':contribution',
            token=friendo['token']
        )
        # we can ack it directly as it should not be taken in account until
        # we run crons
        validate_all([main1], [c1])
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])
    with main2, other2:
        [c2] = other2.make_commits(
            main2.commit('a').id, Commit('commit repo 2', tree={'2': 'a'}),
            ref='heads/contribution' # use same ref / label as pr1
        )
        pr2 = main2.make_pr(
            target='a', title="Main2 part of my contribution",
            head=friendo['user'] + ':contribution',
            token=friendo['token']
        )
        validate_all([main2], [c2])
        pr2.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()

    # sanity check: this should have created a staging with 1 batch with pr1 and pr2
    stagings = env['runbot_merge.stagings'].search([])
    assert len(stagings) == 1
    assert stagings.target.name == 'a'
    assert len(stagings.batch_ids) == 1
    assert stagings.mapped('batch_ids.prs.number') == [pr1.number, pr2.number]

    with main1, main2:
        validate_all([main1, main2], ['staging.a'])
    env.run_crons()

    PullRequests = env['runbot_merge.pull_requests']
    # created the first forward port, need to validate it so the second one is
    # triggered (FP only goes forward on CI+) (?)
    pr1b = PullRequests.search([
        ('source_id', '!=', False),
        ('repository.name', '=', main1.name),
    ])
    pr2b = PullRequests.search([
        ('source_id', '!=', False),
        ('repository.name', '=', main2.name),
    ])
    # check that relevant users were pinged
    ping = (users['user'], """\
This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""")
    pr_remote_1b = main1.get_pr(pr1b.number)
    pr_remote_2b = main2.get_pr(pr2b.number)
    assert pr_remote_1b.comments == [seen(env, pr_remote_1b, users), ping]
    assert pr_remote_2b.comments == [seen(env, pr_remote_2b, users), ping]

    with main1, main2:
        validate_all([main1], [pr1b.head])
        validate_all([main2], [pr2b.head])
    env.run_crons() # process updated statuses -> generate followup FP

    # should have created two PRs whose source is p1 and two whose source is p2
    pr1a, pr1b, pr1c = PullRequests.search([
        ('repository.name', '=', main1.name),
    ], order='number')
    pr2a, pr2b, pr2c = PullRequests.search([
        ('repository.name', '=', main2.name),
    ], order='number')

    assert pr1a.number == pr1.number
    assert pr2a.number == pr2.number
    assert pr1a.state == pr2a.state == 'merged'

    assert pr1b.label == pr2b.label, "batched source should yield batched FP"
    assert pr1c.label == pr2c.label, "batched source should yield batched FP"
    assert pr1b.label != pr1c.label

    # ok main1 PRs
    with main1:
        validate_all([main1], [pr1c.head])
        main1.get_pr(pr1c.number).post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    # check that the main1 PRs are ready but blocked on the main2 PRs
    assert pr1b.state == 'ready'
    assert pr1c.state == 'ready'
    assert pr1b.blocked
    assert pr1c.blocked

    # ok main2 PRs
    with main2:
        validate_all([main2], [pr2c.head])
        main2.get_pr(pr2c.number).post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    env['runbot_merge.stagings'].search([]).mapped('target.display_name')
    env['runbot_merge.stagings'].search([], order='target').mapped('target.display_name')
    stc, stb = env['runbot_merge.stagings'].search([], order='target')
    assert stb.target.name == 'b'
    assert stc.target.name == 'c'

    with main1, main2:
        validate_all([main1, main2], ['staging.b', 'staging.c'])

def test_fw_nice(env, config, users, make_basic):
    prod, _ = make_basic()
    env['runbot_merge.project'].search([]).fw_nice = True
    with prod:
        prod.make_commits("a", Commit("p", tree={"x": "0"}), ref="heads/earl")
        pr0 = prod.make_pr(target="a", head="earl")
        prod.post_status("earl", "success")
        pr0.post_comment("hansen r+", config["role_reviewer"]["token"])
    env.run_crons()

    with prod:
        prod.post_status("staging.a", "success")
    env.run_crons()
    _, pr1_id = env["runbot_merge.pull_requests"].search([], order="number asc")
    assert pr1_id.batch_id.priority == 'nice'
    pr1_id.batch_id.priority = 'default'

    # descendants of the fw just inherit the priority it has, even if it's just
    # reset to default
    with prod:
        prod.post_status(pr1_id.head, "success")
    env.run_crons()
    _, _, pr2_id = env["runbot_merge.pull_requests"].search([], order="number asc")
    assert pr2_id.batch_id.priority == 'default'

class TestClosing:
    def test_closing_before_fp(self, env, config, users, make_basic):
        """ Closing a PR should preclude its forward port
        """
        prod, _other = make_basic()
        with prod:
            [p_1] = prod.make_commits(
                'a',
                Commit('p_0', tree={'x': '0'}),
                ref='heads/hugechange'
            )
            pr = prod.make_pr(target='a', head='hugechange')
            prod.post_status(p_1, 'success')
            pr.post_comment('hansen r+', config['role_reviewer']['token'])

        env.run_crons()
        with prod:
            prod.post_status('staging.a', 'success')
        # should merge the staging then create the FP PR
        env.run_crons()

        pr0_id, pr1_id = env['runbot_merge.pull_requests'].search([], order='number')
        # close the FP PR then have CI validate it
        pr1 = prod.get_pr(pr1_id.number)
        with prod:
            pr1.close()
        assert pr1_id.state == 'closed'
        assert not pr1_id.parent_id, "closed PR should should be detached from its parent"
        with prod:
            prod.post_status(pr1_id.head, 'success')
        env.run_crons()
        env.run_crons('runbot_merge.reminder')

        assert env['runbot_merge.pull_requests'].search([], order='number') == pr0_id | pr1_id,\
            "closing the PR should suppress the FP sequence"
        assert pr1.comments == [
            seen(env, pr1, users),
            (users['user'], """\
This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""")
        ]

    def test_closing_after_fp(self, env, config, users, make_basic):
        """ Closing a PR which has been forward-ported should not touch the
        followups
        """
        prod, _other = make_basic()
        with prod:
            [p_1] = prod.make_commits(
                'a',
                Commit('p_0', tree={'x': '0'}),
                ref='heads/hugechange'
            )
            pr = prod.make_pr(target='a', head='hugechange')
            prod.post_status(p_1, 'success')
            pr.post_comment('hansen r+', config['role_reviewer']['token'])

        env.run_crons()
        with prod:
            prod.post_status('staging.a', 'success')

        # should merge the staging then create the FP PR
        env.run_crons()

        pr0_id, pr1_id = env['runbot_merge.pull_requests'].search([], order='number')
        with prod:
            prod.post_status(pr1_id.head, 'success')
        # should create the second staging
        env.run_crons()

        pr0_id2, pr1_id2, pr2_id = env['runbot_merge.pull_requests'].search([], order='number')
        assert pr0_id2 == pr0_id
        assert pr1_id2 == pr1_id

        pr1 = prod.get_pr(pr1_id.number)
        with prod:
            pr1.close()

        assert pr1_id.state == 'closed'
        assert not pr1_id.parent_id
        assert pr2_id.state == 'opened'
        assert not pr2_id.parent_id, \
            "the descendant of a closed PR doesn't really make sense, maybe?"
        pr2 = prod.get_pr(pr2_id.number)
        assert pr2.comments == [
            seen(env, pr2, users),
            (users['user'], """\
@{user} @{reviewer} this PR targets c and is the last of the forward-port chain containing:
* {pr1_id.display_name}

To merge the full chain, use
> @hansen r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""".format(pr1_id=pr1_id, **users)),
        ]

        with prod:
            pr1.open()
        assert pr1_id.state == 'validated'
        assert not pr1_id.parent_id
        assert not pr2_id.parent_id

    def test_closing_after_fp_second(self, env, config, users, make_basic):
        """If we close a forward-ported PR whose child has a child, then we don't
        need to send a notification on the child: while it is technically detached,
        said detachment has no effect as it does not need forward porting anymore.
        """
        # region setup
        prod, _ = make_basic()
        # region add one more branch to the project
        proj = env['runbot_merge.project'].search([])
        proj.write({'branch_ids': [(0, 0, {'name': 'd', 'sequence': 40})]})
        with prod:
            prod.make_commits(
                'c',
                Commit('1111', tree={'i': 'a'}),
                ref='heads/d',
            )
        # endregion
        with prod:
            prod.make_commits('a', Commit('pr', tree={'x': '0'}), ref="heads/abranch")
            pr_a = prod.make_pr(target='a', head='abranch')
            prod.post_status('abranch', 'success')
            pr_a.post_comment('hansen r+ fw=skipci', config['role_reviewer']['token'])
        env.run_crons()
        with prod:
            prod.post_status('staging.a', 'success')
        env.run_crons()

        pr_a_id, pr_b_id, pr_c_id, pr_d_id = env['runbot_merge.pull_requests'].search([], order='number')
        assert pr_d_id.parent_id == pr_c_id
        assert pr_c_id.parent_id == pr_b_id
        assert pr_b_id.parent_id == pr_a_id
        # endregion

        with prod:
            prod.get_pr(pr_b_id.number).close()
        assert pr_b_id.closed
        assert not pr_c_id.parent_id
        prc = prod.get_pr(pr_c_id.number)
        assert prc.comments == [
            seen(env, prc, users),
            (users['user'], """\
This PR targets c and is part of the forward-port chain. Further PRs will be created up to d.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
"""),
        ]

    def test_close_disabled(self, env, users, config, make_basic):
        """ If an fwport's target is disabled and its branch is closed, it
        should not be notified (multiple times), also its descendant should not
        be nodified if already merged, also there should not be recursive
        notifications (odoo/odoo#145969, odoo/odoo#145984)
        """
        repo, _ = make_basic()
        # prep: merge PR, create two forward ports
        with repo:
            [c1] = repo.make_commits('a', Commit('first', tree={'m': 'c1'}))
            pr1 = repo.make_pr(title='title', body='body', target='a', head=c1)
            pr1.post_comment('hansen r+', config['role_reviewer']['token'])
            repo.post_status(c1, 'success')
        env.run_crons()

        pr1_id = to_pr(env, pr1)
        assert pr1_id.state == 'ready', pr1_id.blocked

        with repo:
            repo.post_status('staging.a', 'success')
        env.run_crons()

        pr1_id_, pr2_id = env['runbot_merge.pull_requests'].search([], order='number')
        assert pr1_id_ == pr1_id
        with repo:
            repo.post_status(pr2_id.head, 'success')
        env.run_crons()

        _, _, pr3_id = env['runbot_merge.pull_requests'].search([], order='number')

        # disable second branch
        pr2_id.target.active = False
        env.run_crons()

        pr2 = repo.get_pr(pr2_id.number)
        assert pr2.comments == [
            seen(env, pr2, users),
            (users['user'], "This PR targets b and is part of the forward-port chain. "
                           "Further PRs will be created up to c.\n\n"
                           "More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port\n"),
            (users['user'], "@{user} @{reviewer} the target branch 'b' has been disabled, you may want to close this PR.".format_map(
                users
            )),
        ]
        pr3 = repo.get_pr(pr3_id.number)
        assert pr3.comments == [
            seen(env, pr3, users),
            (users['user'], """\
@{user} @{reviewer} this PR targets c and is the last of the forward-port chain containing:
* {pr2_id.display_name}

To merge the full chain, use
> @hansen r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""".format(pr2_id=pr2_id, **users)),
        ]

        # some time later, notice PR3 is open and merge it
        with repo:
            pr3.post_comment('hansen r+', config['role_reviewer']['token'])
            repo.post_status(pr3.head, 'success')
        env.run_crons()
        with repo:
            repo.post_status('staging.c', 'success')
        env.run_crons()

        assert pr3_id.status == 'success'

        # even later, notice PR2 is still open but not mergeable anymore
        with repo:
            pr2.close()
        env.run_crons()

        assert pr2.comments[3:] == []
        assert pr3.comments[2:] == [(users['reviewer'], "hansen r+")]

class TestBranchDeletion:
    def test_delete_normal(self, env, config, make_basic):
        """ Regular PRs should get their branch deleted as long as they're
        created in the fp repository
        """
        prod, other = make_basic()
        with prod, other:
            [c] = other.make_commits(prod.commit('a').id, Commit('c', tree={'0': '0'}), ref='heads/abranch')
            pr = prod.make_pr(
                target='a', head='%s:abranch' % other.owner,
                title="a pr",
            )
            prod.post_status(c, 'success')
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with prod:
            prod.post_status('staging.a', 'success')
        env.run_crons()

        pr_id = to_pr(env, pr)
        assert pr_id.state == 'merged'
        removers = env['forwardport.branch_remover'].search([])
        to_delete_branch = removers.mapped('pr_id')
        assert to_delete_branch == pr_id

        env.run_crons('runbot_merge.remover', context={'forwardport_merged_before': FAKE_PREV_WEEK})
        with pytest.raises(AssertionError, match="Not Found"):
            other.get_ref('heads/abranch')

    def test_not_merged(self, env, config, make_basic):
        """ The branches of PRs which are still open should not get deleted, but closed ones should
        """
        prod, other = make_basic()
        a_ref = prod.commit('a').id
        with prod, other:
            [c1] = other.make_commits(a_ref, Commit('c1', tree={'1': '0'}), ref='heads/abranch')
            pr1 = prod.make_pr(target='a', head=f'{other.owner}:abranch', title='a')

            other.make_commits(a_ref, Commit('c2', tree={'2': '0'}), ref='heads/bbranch')
            pr2 = prod.make_pr(target='a', head=f'{other.owner}:bbranch', title='b')

            [c3] = other.make_commits(a_ref, Commit('c3', tree={'3': '0'}), ref='heads/cbranch')
            pr3 = prod.make_pr(target='a', head=f'{other.owner}:cbranch', title='c')

            other.make_commits(a_ref, Commit('c3', tree={'4': '0'}), ref='heads/dbranch')
            pr4 = prod.make_pr(target='a', head=f'{other.owner}:dbranch', title='d')
        with prod, other:
            prod.post_status(c1, 'success')
            pr1.post_comment('hansen r+', config['role_reviewer']['token'])
            pr2.close()
            prod.post_status(c3, 'success')
            pr4.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        PR = env['runbot_merge.pull_requests']
        # check PRs are in states we expect
        pr_heads = []
        for p, st in [(pr1, 'ready'), (pr2, 'closed'), (pr3, 'validated'), (pr4, 'approved')]:
            p_id = PR.search([
                ('repository.name', '=', prod.name),
                ('number', '=', p.number),
            ])
            assert p_id.state == st
            pr_heads.append(p_id.head)

        env.run_crons('runbot_merge.remover', context={'forwardport_merged_before': FAKE_PREV_WEEK})

        # check that the branches still exist
        assert other.get_ref('heads/abranch') == pr_heads[0]
        with pytest.raises(AssertionError, match="Not Found"):
            other.get_ref('heads/bbranch')
        assert other.get_ref('heads/cbranch') == pr_heads[2]
        assert other.get_ref('heads/dbranch') == pr_heads[3]

    def test_reused(self, env, config, make_basic):
        """If a PR is closed then a new PR is created on the same branch, said branch should not be deleted
        """
        prod, other = make_basic()
        with prod, other:
            other.make_commits(prod.commit('a').id, Commit('c', tree={'0': '0'}), ref='heads/abranch')
            pr1 = prod.make_pr(target='a', head=f'{other.owner}:abranch', title="a pr")
        env.run_crons()

        with prod:
            pr1.close()
        env.run_crons()

        with prod:
            pr2 = prod.make_pr(target='a', head=f'{other.owner}:abranch', title="a pr v2")
        env.run_crons()
        assert pr2.number != pr1.number, "we should have created a new PR"

        env.run_crons('runbot_merge.remover', context={'forwardport_merged_before': FAKE_PREV_WEEK})

        assert other.get_ref('heads/abranch') == to_pr(env, pr2).head

    def test_updated(self, env, config, make_basic):
        """If a PR is closed then its branch is updated, the branch should not be deleted
        """
        prod, other = make_basic()
        with prod, other:
            other.make_commits(prod.commit('a').id, Commit('c', tree={'0': '0'}), ref='heads/abranch')
            pr1 = prod.make_pr(target='a', head=f'{other.owner}:abranch', title="a pr")
        env.run_crons()

        with prod:
            pr1.close()
        env.run_crons()

        with other:
            [c] = other.make_commits('abranch', Commit('d', tree={'0': '1'}), ref='heads/abranch', make=False)
        env.run_crons()

        env.run_crons('runbot_merge.remover', context={'forwardport_merged_before': FAKE_PREV_WEEK})

        assert other.get_ref('heads/abranch') == c


def sPeNgBaB(s):
    return ''.join(
        l if i % 2 == 0 else l.upper()
        for i, l in enumerate(s)
    )
def test_spengbab():
    assert sPeNgBaB("spongebob") == 'sPoNgEbOb'

class TestRecognizeCommands:
    def make_pr(self, env, make_basic):
        r, _ = make_basic()

        with r:
            r.make_commits('c', Commit('p', tree={'x': '0'}), ref='heads/testbranch')
            pr = r.make_pr(target='a', head='testbranch')

        return r, pr, to_pr(env, pr)

    # FIXME: remove / merge into mergebot tests
    def test_botname_casing(self, env, config, make_basic):
        """ Test that the botname is case-insensitive as people might write
        bot names capitalised or titlecased or uppercased or whatever
        """
        repo, pr, pr_id = self.make_pr(env, make_basic)
        assert pr_id.state == 'opened'
        [a] = env['runbot_merge.branch'].search([
            ('name', '=', 'a')
        ])

        names = [
            "hansen",
            "HANSEN",
            "Hansen",
            sPeNgBaB("hansen"),
        ]

        for n in names:
            assert not pr_id.limit_id
            with repo:
                pr.post_comment(f'@{n} up to a', config['role_reviewer']['token'])
            assert pr_id.limit_id == a
            # reset state
            pr_id.limit_id = False

    # FIXME: remove / merge into mergebot tests
    @pytest.mark.parametrize('indent', ['', '\N{SPACE}', '\N{SPACE}'*4, '\N{TAB}'])
    def test_botname_indented(self, env, config, make_basic, indent):
        """ matching botname should ignore leading whitespaces
        """
        repo, pr, pr_id = self.make_pr(env, make_basic)
        assert pr_id.state == 'opened'
        [a] = env['runbot_merge.branch'].search([
            ('name', '=', 'a')
        ])

        assert not pr_id.limit_id
        with repo:
            pr.post_comment(f'{indent}@hansen up to a', config['role_reviewer']['token'])
        assert pr_id.limit_id == a

def test_reminders(env, config, users, make_basic):
    prod, _ = make_basic()
    with prod:
        prod.make_commits('a', Commit('c', tree={'x': '0'}), ref='heads/hugechange')
        pra = prod.make_pr(target='a', head='hugechange')
        prod.post_status(pra.head, 'success')
        pra.post_comment('hansen remindme:b=firstmessage remindme:c="second message"', config['role_other']['token'])
        pra.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    branches = env['runbot_merge.branch'].search([], order='name')
    pra_id = to_pr(env, pra)
    assert pra_id.state == 'merged'
    assert pra_id.fw_reminder_ids.mapped(attrgetter('partner_id.name', 'branch_id', 'message')) == [
        (users['other'], branches[1], "firstmessage"),
        (users['other'], branches[2], "second message"),
    ]

    prb_id = pra_id.forwardport_ids
    prb = prod.get_pr(prb_id.number)
    with prod:
        prod.post_status(prb.head, 'success')
        prb.post_comment('hansen remindme:b="initial message"', config['role_self_reviewer']['token'])
        prb.post_comment("hansen remindme:c='other message'", config['role_self_reviewer']['token'])
        prb.post_comment("hansen remindme:x='some message'", config['role_self_reviewer']['token'])
    env.run_crons()

    assert pra_id.fw_reminder_ids.mapped(attrgetter('partner_id.name', 'branch_id', 'message')) == [
        (users['other'], branches[1], "firstmessage"),
        (users['other'], branches[2], "second message"),
        (users['self_reviewer'], branches[2], "other message"),
    ]
    assert prb.comments == [
        seen(env, prb, users),
        (users['user'], """\
This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
"""),
        (users['user'], f"@{users['other']} firstmessage"),
        (users['self_reviewer'], 'hansen remindme:b="initial message"'),
        (users['self_reviewer'], "hansen remindme:c='other message'"),
        (users['self_reviewer'], "hansen remindme:x='some message'"),
        (users['user'], f"@{users['self_reviewer']} pr is already ported to 'b', I can't remind you for that"),
        (users['user'], f"@{users['self_reviewer']} unknown or invalid branch 'x'"),
    ]

    prc_id = pra_id.forwardport_ids - prb_id
    prc = prod.get_pr(prc_id.number)

    assert prc.comments == [
        seen(env, prc, users),
        (users['user'], f"""\
@{users['user']} @{users['reviewer']} this PR targets c and is the last of the forward-port chain containing:
* {prb_id.display_name}

To merge the full chain, use
> @hansen r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
"""),
        (users['user'], f'@{users['other']} second message\n@{users['self_reviewer']} other message'),
    ]
