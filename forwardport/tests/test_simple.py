# -*- coding: utf-8 -*-
import collections
import re
import time
from datetime import datetime, timedelta

import pytest

from utils import seen, Commit, make_basic, REF_PATTERN, MESSAGE_TEMPLATE, validate_all, part_of

FMT = '%Y-%m-%d %H:%M:%S'
FAKE_PREV_WEEK = (datetime.now() + timedelta(days=1)).strftime(FMT)

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
def test_straightforward_flow(env, config, make_repo, users):
    # TODO: ~all relevant data in users when creating partners
    # get reviewer's name
    reviewer_name = env['res.partner'].search([
        ('github_login', '=', users['reviewer'])
    ]).name

    prod, other = make_basic(env, config, make_repo)
    other_user = config['role_other']
    other_user_repo = prod.fork(token=other_user['token'])

    project = env['runbot_merge.project'].search([])
    b_head = prod.commit('b')
    c_head = prod.commit('c')
    with prod, other_user_repo:
        # create PR as a user with no access to prod (or other)
        [_, p_1] = other_user_repo.make_commits(
            'a',
            Commit('p_0', tree={'x': '0'}),
            Commit('p_1', tree={'x': '1'}),
            ref='heads/hugechange'
        )
        pr = prod.make_pr(
            target='a', title="super important change",
            head=other_user['user'] + ':hugechange',
            token=other_user['token']
        )
        prod.post_status(p_1, 'success', 'legal/cla')
        prod.post_status(p_1, 'success', 'ci/runbot')
        # use rebase-ff (instead of rebase-merge) so we don't have to dig in
        # parents of the merge commit to find the cherrypicks
        pr.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])
    pr_id = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', prod.name),
        ('number', '=', pr.number),
    ])
    assert not pr_id.merge_date,\
        "PR obviously shouldn't have a merge date before being merged"

    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

    # should merge the staging then create the FP PR
    env.run_crons()

    assert datetime.utcnow() - datetime.strptime(pr_id.merge_date, FMT) <= timedelta(minutes=1),\
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
        prod.post_status(pr1.head, 'success', 'ci/runbot')
        prod.post_status(pr1.head, 'success', 'legal/cla')

    env.run_crons()
    env.run_crons('forwardport.reminder', 'runbot_merge.feedback_cron', context={'forwardport_updated_before': FAKE_PREV_WEEK})

    pr0_, pr1_, pr2 = env['runbot_merge.pull_requests'].search([], order='number')

    assert pr.comments == [
        (users['reviewer'], 'hansen r+ rebase-ff'),
        seen(env, pr, users),
        (users['user'], 'Merge method set to rebase and fast-forward.'),
        (users['user'], '@%s @%s this pull request has forward-port PRs awaiting action (not merged or closed):\n- %s' % (
            users['other'], users['reviewer'],
            '\n- '.join((pr1 | pr2).mapped('display_name'))
        )),
    ]

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
> @%s r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""" % (
            users['other'], users['reviewer'],
            pr1.display_name,
            project.fp_github_name
    )),
    ]
    with prod:
        prod.post_status(pr2.head, 'success', 'ci/runbot')
        prod.post_status(pr2.head, 'success', 'legal/cla')

        pr2_remote.post_comment('%s r+' % project.fp_github_name, config['role_reviewer']['token'])

    env.run_crons()

    assert pr1.staging_id
    assert pr2.staging_id
    # two branches so should have two stagings
    assert pr1.staging_id != pr2.staging_id
    # validate
    with prod:
        prod.post_status('staging.b', 'success', 'ci/runbot')
        prod.post_status('staging.b', 'success', 'legal/cla')
        prod.post_status('staging.c', 'success', 'ci/runbot')
        prod.post_status('staging.c', 'success', 'legal/cla')

    # and trigger merge
    env.run_crons()

    # apparently github strips out trailing newlines when fetching through the
    # API...
    message_template = MESSAGE_TEMPLATE.format(
        message='p_1',
        repo=prod.name,
        number='%s',
        headers='X-original-commit: {}\n'.format(p_1_merged.id),
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
    env.run_crons('forwardport.remover', context={'forwardport_merged_before': FAKE_PREV_WEEK})

    # should not have deleted the base branch (wrong repo)
    assert other_user_repo.get_ref(pr.ref) == p_1

    # should have deleted all PR branches
    pr1_ref = prod.get_pr(pr1.number).ref
    with pytest.raises(AssertionError, match='Not Found'):
        other.get_ref(pr1_ref)

    pr2_ref = pr2_remote.ref
    with pytest.raises(AssertionError, match="Not Found"):
        other.get_ref(pr2_ref)

def test_empty(env, config, make_repo, users):
    """ Cherrypick of an already cherrypicked (or separately implemented)
    commit -> conflicting pr.
    """
    prod, other = make_basic(env, config, make_repo)
    # merge change to b
    with prod:
        [p_0] = prod.make_commits(
            'b', Commit('p', tree={'x': '0'}),
            ref='heads/early'
        )
        pr0 = prod.make_pr(target='b', head='early')
        prod.post_status(p_0, 'success', 'legal/cla')
        prod.post_status(p_0, 'success', 'ci/runbot')
        pr0.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.b', 'success', 'legal/cla')
        prod.post_status('staging.b', 'success', 'ci/runbot')

    # merge same change to a afterwards
    with prod:
        [p_1] = prod.make_commits(
            'a', Commit('p_0', tree={'x': '0'}),
            ref='heads/late'
        )
        pr1 = prod.make_pr(target='a', head='late')
        prod.post_status(p_1, 'success', 'legal/cla')
        prod.post_status(p_1, 'success', 'ci/runbot')
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

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
        validate_all([prod], [fp_id.head, fail_id.head])
    env.run_crons()

    # should not have created any new PR
    assert env['runbot_merge.pull_requests'].search([], order='number') == prs
    # change FP token to see if the feedback comes from the proper user
    project = env['runbot_merge.project'].search([])
    project.write({
        'fp_github_name': False,
        'fp_github_email': False,
        'fp_github_token': config['role_other']['token'],
    })
    assert project.fp_github_name == users['other']

    # check reminder
    env.run_crons('forwardport.reminder', 'runbot_merge.feedback_cron', context={'forwardport_updated_before': FAKE_PREV_WEEK})
    env.run_crons('forwardport.reminder', 'runbot_merge.feedback_cron', context={'forwardport_updated_before': FAKE_PREV_WEEK})

    awaiting = (
        users['other'],
        '@%s @%s this pull request has forward-port PRs awaiting action (not merged or closed):\n- %s' % (
            users['user'], users['reviewer'],
            fail_id.display_name
        )
    )
    assert pr1.comments == [
        (users['reviewer'], 'hansen r+'),
        seen(env, pr1, users),
        awaiting,
        awaiting,
    ], "each cron run should trigger a new message on the ancestor"
    # check that this stops if we close the PR
    with prod:
        prod.get_pr(fail_id.number).close()
    env.run_crons('forwardport.reminder', 'runbot_merge.feedback_cron', context={'forwardport_updated_before': FAKE_PREV_WEEK})
    assert pr1.comments == [
        (users['reviewer'], 'hansen r+'),
        seen(env, pr1, users),
        awaiting,
        awaiting,
    ]

def test_partially_empty(env, config, make_repo):
    """ Check what happens when only some commits of the PR are now empty
    """
    prod, other = make_basic(env, config, make_repo)
    # merge change to b
    with prod:
        [p_0] = prod.make_commits(
            'b', Commit('p', tree={'x': '0'}),
            ref='heads/early'
        )
        pr0 = prod.make_pr(target='b', head='early')
        prod.post_status(p_0, 'success', 'legal/cla')
        prod.post_status(p_0, 'success', 'ci/runbot')
        pr0.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.b', 'success', 'legal/cla')
        prod.post_status('staging.b', 'success', 'ci/runbot')

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
        prod.post_status(p_1, 'success', 'legal/cla')
        prod.post_status(p_1, 'success', 'ci/runbot')
        pr1.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

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
def test_access_rights(env, config, make_repo, users, author, reviewer, delegate, success):
    """Validates the review rights *for the forward-port sequence*, the original
    PR is always reviewed by `user`.
    """
    prod, other = make_basic(env, config, make_repo)
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
        [c] = fork.make_commits('a', Commit('c_0', tree={'y': '0'}), ref='heads/accessrights')
        pr = prod.make_pr(
            target='a', title='my change',
            head=users[author] + ':accessrights',
            token=author_token,
        )
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', token=config['github']['token'])
        if delegate:
            pr.post_comment('hansen delegate=%s' % users[delegate], token=config['github']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()

    pr0, pr1 = env['runbot_merge.pull_requests'].search([], order='number')
    assert pr0.state == 'merged'
    with prod:
        prod.post_status(pr1.head, 'success', 'ci/runbot')
        prod.post_status(pr1.head, 'success', 'legal/cla')
    env.run_crons()

    _, _, pr2 = env['runbot_merge.pull_requests'].search([], order='number')
    with prod:
        prod.post_status(pr2.head, 'success', 'ci/runbot')
        prod.post_status(pr2.head, 'success', 'legal/cla')
        prod.get_pr(pr2.number).post_comment(
            '%s r+' % project.fp_github_name,
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


def test_delegate_fw(env, config, make_repo, users):
    """If a user is delegated *on a forward port* they should be able to approve
    *the followup*.
    """
    prod, _ = make_basic(env, config, make_repo)
    # create a partner for `other` so we can put an email on it
    env['res.partner'].create({
        'name': users['other'],
        'github_login': users['other'],
        'email': 'other@example.org',
    })
    author_token = config['role_self_reviewer']['token']
    fork = prod.fork(token=author_token)
    with prod, fork:
        [c] = fork.make_commits('a', Commit('c_0', tree={'y': '0'}), ref='heads/accessrights')
        pr = prod.make_pr(
            target='a', title='my change',
            head=users['self_reviewer'] + ':accessrights',
            token=author_token,
        )
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', token=config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()

    # ensure pr1 has to be approved to be forward-ported
    _, pr1_id = env['runbot_merge.pull_requests'].search([], order='number')
    # detatch from source
    pr1_id.write({
        'parent_id': False,
        'detach_reason': "Detached for testing.",
    })
    with prod:
        prod.post_status(pr1_id.head, 'success', 'legal/cla')
        prod.post_status(pr1_id.head, 'success', 'ci/runbot')
    env.run_crons()
    pr1 = prod.get_pr(pr1_id.number)
    # delegate review to "other" consider PR fixed, and have "other" approve it
    with prod:
        pr1.post_comment('hansen delegate=' + users['other'],
                         token=config['role_reviewer']['token'])
        prod.post_status(pr1_id.head, 'success', 'ci/runbot')
        pr1.post_comment('hansen r+', token=config['role_other']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.b', 'success', 'legal/cla')
        prod.post_status('staging.b', 'success', 'ci/runbot')
    env.run_crons()

    _, _, pr2_id = env['runbot_merge.pull_requests'].search([], order='number')
    pr2 = prod.get_pr(pr2_id.number)
    # make "other" also approve this one
    with prod:
        prod.post_status(pr2_id.head, 'success', 'ci/runbot')
        prod.post_status(pr2_id.head, 'success', 'legal/cla')
        pr2.post_comment('hansen r+', token=config['role_other']['token'])
    env.run_crons()

    assert pr2.comments == [
        seen(env, pr2, users),
        (users['user'], '''@{self_reviewer} @{reviewer} this PR targets c and is the last of the forward-port chain.

To merge the full chain, use
> @{bot} r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
'''.format(bot=pr1_id.repository.project_id.fp_github_name, **users)),
        (users['other'], 'hansen r+')
    ]


def test_redundant_approval(env, config, make_repo, users):
    """If a forward port sequence has been partially approved, fw-bot r+ should
    not perform redundant approval as that triggers warning messages.
    """
    prod, _ = make_basic(env, config, make_repo)
    [project] = env['runbot_merge.project'].search([])
    with prod:
        prod.make_commits(
            'a', Commit('p', tree={'x': '0'}),
            ref='heads/early'
        )
        pr0 = prod.make_pr(target='a', head='early')
        prod.post_status('heads/early', 'success', 'legal/cla')
        prod.post_status('heads/early', 'success', 'ci/runbot')
        pr0.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()
    pr0_id, pr1_id = env['runbot_merge.pull_requests'].search([], order='number asc')
    with prod:
        prod.post_status(pr1_id.head, 'success', 'legal/cla')
        prod.post_status(pr1_id.head, 'success', 'ci/runbot')
    env.run_crons()

    _, _, pr2_id = env['runbot_merge.pull_requests'].search([], order='number asc')
    assert pr2_id.parent_id == pr1_id
    assert pr1_id.parent_id == pr0_id

    pr1 = prod.get_pr(pr1_id.number)
    pr2 = prod.get_pr(pr2_id.number)
    with prod:
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])
    with prod:
        pr2.post_comment(f'{project.fp_github_name} r+', config['role_reviewer']['token'])
    env.run_crons()

    assert pr1.comments == [
        seen(env, pr1, users),
        (users['user'], 'This PR targets b and is part of the forward-port chain. '
                        'Further PRs will be created up to c.\n\n'
                        'More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port\n'),
        (users['reviewer'], 'hansen r+'),
    ]


def test_batched(env, config, make_repo, users):
    """ Tests for projects with multiple repos & sync'd branches. Batches
    should be FP'd to batches
    """
    main1, _ = make_basic(env, config, make_repo, reponame='main1')
    main2, _ = make_basic(env, config, make_repo, reponame='main2')
    main1.unsubscribe(config['role_reviewer']['token'])
    main2.unsubscribe(config['role_reviewer']['token'])

    friendo = config['role_other']
    other1 = main1.fork(token=friendo['token'])
    other2 = main2.fork(token=friendo['token'])

    with main1, other1:
        [c1] = other1.make_commits(
            'a', Commit('commit repo 1', tree={'1': 'a'}),
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
            'a', Commit('commit repo 2', tree={'2': 'a'}),
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

    project = env['runbot_merge.project'].search([])
    # ok main1 PRs
    with main1:
        validate_all([main1], [pr1c.head])
        main1.get_pr(pr1c.number).post_comment('%s r+' % project.fp_github_name, config['role_reviewer']['token'])
    env.run_crons()

    # check that the main1 PRs are ready but blocked on the main2 PRs
    assert pr1b.state == 'ready'
    assert pr1c.state == 'ready'
    assert pr1b.blocked
    assert pr1c.blocked

    # ok main2 PRs
    with main2:
        validate_all([main2], [pr2c.head])
        main2.get_pr(pr2c.number).post_comment('%s r+' % project.fp_github_name, config['role_reviewer']['token'])
    env.run_crons()

    env['runbot_merge.stagings'].search([]).mapped('target.display_name')
    env['runbot_merge.stagings'].search([], order='target').mapped('target.display_name')
    stc, stb = env['runbot_merge.stagings'].search([], order='target')
    assert stb.target.name == 'b'
    assert stc.target.name == 'c'

    with main1, main2:
        validate_all([main1, main2], ['staging.b', 'staging.c'])

class TestClosing:
    def test_closing_before_fp(self, env, config, make_repo, users):
        """ Closing a PR should preclude its forward port
        """
        prod, other = make_basic(env, config, make_repo)
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
        # close the FP PR then have CI validate it
        pr1 = prod.get_pr(pr1_id.number)
        with prod:
            pr1.close()
        assert pr1_id.state == 'closed'
        assert not pr1_id.parent_id, "closed PR should should be detached from its parent"
        with prod:
            prod.post_status(pr1_id.head, 'success', 'legal/cla')
            prod.post_status(pr1_id.head, 'success', 'ci/runbot')
        env.run_crons()
        env.run_crons('forwardport.reminder', 'runbot_merge.feedback_cron')

        assert env['runbot_merge.pull_requests'].search([], order='number') == pr0_id | pr1_id,\
            "closing the PR should suppress the FP sequence"
        assert pr1.comments == [
            seen(env, pr1, users),
            (users['user'], """\
This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""")
        ]

    def test_closing_after_fp(self, env, config, make_repo, users):
        """ Closing a PR which has been forward-ported should not touch the
        followups
        """
        prod, other = make_basic(env, config, make_repo)
        project = env['runbot_merge.project'].search([])
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
        with prod:
            prod.post_status(pr1_id.head, 'success', 'legal/cla')
            prod.post_status(pr1_id.head, 'success', 'ci/runbot')
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

        with prod:
            pr1.open()
        assert pr1_id.state == 'validated'
        env.run_crons()
        assert pr1.comments[-1] == (
            users['user'],
            "@{} @{} this PR was closed then reopened. "
            "It should be merged the normal way (via @{})".format(
                users['user'],
                users['reviewer'],
                project.github_prefix,
            )
        )

        with prod:
            pr1.post_comment(f'{project.fp_github_name} r+', config['role_reviewer']['token'])
        env.run_crons()
        assert pr1.comments[-1] == (
            users['user'],
            "@{} I can only do this on unmodified forward-port PRs, ask {}.".format(
                users['reviewer'],
                project.github_prefix,
            ),
        )

class TestBranchDeletion:
    def test_delete_normal(self, env, config, make_repo):
        """ Regular PRs should get their branch deleted as long as they're
        created in the fp repository
        """
        prod, other = make_basic(env, config, make_repo)
        with prod, other:
            [c] = other.make_commits('a', Commit('c', tree={'0': '0'}), ref='heads/abranch')
            pr = prod.make_pr(
                target='a', head='%s:abranch' % other.owner,
                title="a pr",
            )
            prod.post_status(c, 'success', 'legal/cla')
            prod.post_status(c, 'success', 'ci/runbot')
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with prod:
            prod.post_status('staging.a', 'success', 'legal/cla')
            prod.post_status('staging.a', 'success', 'ci/runbot')
        env.run_crons()

        pr_id = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', prod.name),
            ('number', '=', pr.number)
        ])
        assert pr_id.state == 'merged'
        removers = env['forwardport.branch_remover'].search([])
        to_delete_branch = removers.mapped('pr_id')
        assert to_delete_branch == pr_id

        env.run_crons('forwardport.remover', context={'forwardport_merged_before': FAKE_PREV_WEEK})
        with pytest.raises(AssertionError, match="Not Found"):
            other.get_ref('heads/abranch')

    def test_not_merged(self, env, config, make_repo):
        """ The branches of PRs which are still open or have been closed (rather
        than merged) should not get deleted
        """
        prod, other = make_basic(env, config, make_repo)
        with prod, other:
            [c] = other.make_commits('a', Commit('c1', tree={'1': '0'}), ref='heads/abranch')
            pr1 = prod.make_pr(target='a', head='%s:abranch' % other.owner, title='a')
            prod.post_status(c, 'success', 'legal/cla')
            prod.post_status(c, 'success', 'ci/runbot')
            pr1.post_comment('hansen r+', config['role_reviewer']['token'])

            other.make_commits('a', Commit('c2', tree={'2': '0'}), ref='heads/bbranch')
            pr2 = prod.make_pr(target='a', head='%s:bbranch' % other.owner, title='b')
            pr2.close()

            [c] = other.make_commits('a', Commit('c3', tree={'3': '0'}), ref='heads/cbranch')
            pr3 = prod.make_pr(target='a', head='%s:cbranch' % other.owner, title='c')
            prod.post_status(c, 'success', 'legal/cla')
            prod.post_status(c, 'success', 'ci/runbot')

            other.make_commits('a', Commit('c3', tree={'4': '0'}), ref='heads/dbranch')
            pr4 = prod.make_pr(target='a', head='%s:dbranch' % other.owner, title='d')
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

        env.run_crons('forwardport.remover', context={'forwardport_merged_before': FAKE_PREV_WEEK})

        # check that the branches still exist
        assert other.get_ref('heads/abranch') == pr_heads[0]
        assert other.get_ref('heads/bbranch') == pr_heads[1]
        assert other.get_ref('heads/cbranch') == pr_heads[2]
        assert other.get_ref('heads/dbranch') == pr_heads[3]

def sPeNgBaB(s):
    return ''.join(
        l if i % 2 == 0 else l.upper()
        for i, l in enumerate(s)
    )
def test_spengbab():
    assert sPeNgBaB("spongebob") == 'sPoNgEbOb'

class TestRecognizeCommands:
    def make_pr(self, env, config, make_repo):
        r, _ = make_basic(env, config, make_repo)

        with r:
            r.make_commits('c', Commit('p', tree={'x': '0'}), ref='heads/testbranch')
            pr = r.make_pr(target='a', head='testbranch')

        return r, pr, env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', r.name),
            ('number', '=', pr.number),
        ])

    def test_botname_casing(self, env, config, make_repo):
        """ Test that the botname is case-insensitive as people might write
        bot names capitalised or titlecased or uppercased or whatever
        """
        repo, pr, pr_id = self.make_pr(env, config, make_repo)
        assert pr_id.state == 'opened'
        botname = env['runbot_merge.project'].search([]).fp_github_name
        [a] = env['runbot_merge.branch'].search([
            ('name', '=', 'a')
        ])
        [c] = env['runbot_merge.branch'].search([
            ('name', '=', 'c')
        ])

        names = [
            botname,
            botname.upper(),
            botname.capitalize(),
            sPeNgBaB(botname),
        ]

        for n in names:
            assert pr_id.limit_id == c
            with repo:
                pr.post_comment('@%s up to a' % n, config['role_reviewer']['token'])
            assert pr_id.limit_id == a
            # reset state
            pr_id.write({'limit_id': c.id})

    @pytest.mark.parametrize('indent', ['', '\N{SPACE}', '\N{SPACE}'*4, '\N{TAB}'])
    def test_botname_indented(self, env, config, make_repo, indent):
        """ matching botname should ignore leading whitespaces
        """
        repo, pr, pr_id = self.make_pr(env, config, make_repo)
        assert pr_id.state == 'opened'
        botname = env['runbot_merge.project'].search([]).fp_github_name
        [a] = env['runbot_merge.branch'].search([
            ('name', '=', 'a')
        ])
        [c] = env['runbot_merge.branch'].search([
            ('name', '=', 'c')
        ])

        assert pr_id.limit_id == c
        with repo:
            pr.post_comment('%s@%s up to a' % (indent, botname), config['role_reviewer']['token'])
        assert pr_id.limit_id == a
