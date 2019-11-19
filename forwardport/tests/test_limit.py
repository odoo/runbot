# -*- coding: utf-8 -*-
import collections

import pytest

from utils import *

Description = collections.namedtuple('Restriction', 'source limit')
def test_configure(env, config, make_repo):
    """ Checks that configuring an FP limit on a PR is respected

    * limits to not the latest
    * limits to the current target (= no FP)
    * limits to an earlier branch (???)
    """
    prod, other = make_basic(env, config, make_repo)
    bot_name = env['runbot_merge.project'].search([]).fp_github_name
    descriptions = [
        Description(source='a', limit='b'),
        Description(source='b', limit='b'),
        Description(source='b', limit='a'),
    ]
    originals = []
    with prod:
        for i, descr in enumerate(descriptions):
            [c] = prod.make_commits(
                descr.source, Commit('c %d' % i, tree={str(i): str(i)}),
                ref='heads/branch%d' % i,
            )
            pr = prod.make_pr(target=descr.source, head='branch%d'%i)
            prod.post_status(c, 'success', 'legal/cla')
            prod.post_status(c, 'success', 'ci/runbot')
            pr.post_comment('hansen r+\n%s up to %s' % (bot_name, descr.limit), config['role_reviewer']['token'])
            originals.append(pr.number)
    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
        prod.post_status('staging.b', 'success', 'legal/cla')
        prod.post_status('staging.b', 'success', 'ci/runbot')
    env.run_crons()

    # should have created a single FP PR for 0, none for 1 and none for 2
    prs = env['runbot_merge.pull_requests'].search([], order='number')
    assert len(prs) == 4
    assert prs[-1].parent_id == prs[0]
    assert prs[0].number == originals[0]
    assert prs[1].number == originals[1]
    assert prs[2].number == originals[2]


def test_self_disabled(env, config, make_repo):
    """ Allow setting target as limit even if it's disabled
    """
    prod, other = make_basic(env, config, make_repo)
    bot_name = env['runbot_merge.project'].search([]).fp_github_name
    branch_a = env['runbot_merge.branch'].search([('name', '=', 'a')])
    branch_a.fp_target = False
    with prod:
        [c] = prod.make_commits('a', Commit('c', tree={'0': '0'}), ref='heads/mybranch')
        pr = prod.make_pr(target='a', head='mybranch')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+\n%s up to a' % bot_name, config['role_reviewer']['token'])
    env.run_crons()
    pr_id = env['runbot_merge.pull_requests'].search([('number', '=', pr.number)])
    assert pr_id.limit_id == branch_a

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

    assert env['runbot_merge.pull_requests'].search([]) == pr_id,\
        "should not have created a forward port"


def test_ignore(env, config, make_repo):
    """ Provide an "ignore" command which is equivalent to setting the limit
    to target
    """
    prod, other = make_basic(env, config, make_repo)
    bot_name = env['runbot_merge.project'].search([]).fp_github_name
    branch_a = env['runbot_merge.branch'].search([('name', '=', 'a')])
    with prod:
        [c] = prod.make_commits('a', Commit('c', tree={'0': '0'}), ref='heads/mybranch')
        pr = prod.make_pr(target='a', head='mybranch')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+\n%s ignore' % bot_name, config['role_reviewer']['token'])
    env.run_crons()
    pr_id = env['runbot_merge.pull_requests'].search([('number', '=', pr.number)])
    assert pr_id.limit_id == branch_a

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

    assert env['runbot_merge.pull_requests'].search([]) == pr_id,\
        "should not have created a forward port"


@pytest.mark.parametrize('enabled', ['active', 'fp_target'])
def test_disable(env, config, make_repo, users, enabled):
    """ Checks behaviour if the limit target is disabled:

    * disable target while FP is ongoing -> skip over (and stop there so no FP)
    * forward-port over a disabled branch
    * request a disabled target as limit

    Disabling (with respect to forward ports) can be performed by marking the
    branch as !active (which also affects mergebot operations), or as
    !fp_target (won't be forward-ported to).
    """
    prod, other = make_basic(env, config, make_repo)
    project = env['runbot_merge.project'].search([])
    bot_name = project.fp_github_name
    with prod:
        [c] = prod.make_commits('a', Commit('c 0', tree={'0': '0'}), ref='heads/branch0')
        pr = prod.make_pr(target='a', head='branch0')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+\n%s up to b' % bot_name, config['role_reviewer']['token'])

        [c] = prod.make_commits('a', Commit('c 1', tree={'1': '1'}), ref='heads/branch1')
        pr = prod.make_pr(target='a', head='branch1')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    # disable branch b
    env['runbot_merge.branch'].search([('name', '=', 'b')]).write({enabled: False})
    env.run_crons()

    # should have created a single PR (to branch c, for pr 1)
    _0, _1, p = env['runbot_merge.pull_requests'].search([], order='number')
    assert p.parent_id == _1
    assert p.target.name == 'c'

    project.fp_github_token = config['role_other']['token']
    bot_name = project.fp_github_name
    with prod:
        [c] = prod.make_commits('a', Commit('c 2', tree={'2': '2'}), ref='heads/branch2')
        pr = prod.make_pr(target='a', head='branch2')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+\n%s up to' % bot_name, config['role_reviewer']['token'])
        pr.post_comment('%s up to b' % bot_name, config['role_reviewer']['token'])
        pr.post_comment('%s up to foo' % bot_name, config['role_reviewer']['token'])
        pr.post_comment('%s up to c' % bot_name, config['role_reviewer']['token'])
    env.run_crons()

    # use a set because git webhooks delays might lead to mis-ordered
    # responses and we don't care that much
    assert set(pr.comments) == {
        (users['reviewer'], "hansen r+\n%s up to" % bot_name),
        (users['reviewer'], "%s up to b" % bot_name),
        (users['reviewer'], "%s up to foo" % bot_name),
        (users['reviewer'], "%s up to c" % bot_name),
        (users['other'], "Please provide a branch to forward-port to."),
        (users['other'], "Branch 'b' is disabled, it can't be used as a forward port target."),
        (users['other'], "There is no branch 'foo', it can't be used as a forward port target."),
        (users['other'], "Forward-porting to 'c'."),
    }


def test_default_disabled(env, config, make_repo, users):
    """ If the default limit is disabled, it should still be the default
    limit but the ping message should be set on the actual last FP (to the
    last non-deactivated target)
    """
    prod, other = make_basic(env, config, make_repo)
    branch_c = env['runbot_merge.branch'].search([('name', '=', 'c')])
    branch_c.fp_target = False

    with prod:
        [c] = prod.make_commits('a', Commit('c', tree={'0': '0'}), ref='heads/branch0')
        pr = prod.make_pr(target='a', head='branch0')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    assert env['runbot_merge.pull_requests'].search([]).limit_id == branch_c

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()

    p1, p2 = env['runbot_merge.pull_requests'].search([], order='number')
    assert p1.number == pr.number
    pr2 = prod.get_pr(p2.number)

    cs = pr2.comments
    assert len(cs) == 1
    assert pr2.comments == [
        (users['user'], """\
Ping @%s, @%s
This PR targets b and is the last of the forward-port chain.

To merge the full chain, say
> @%s r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""" % (users['user'], users['reviewer'], users['user'])),
    ]

def test_limit_after_merge(env, config, make_repo, users):
    """ If attempting to set a limit (<up to>) on a PR which is merged
    (already forward-ported or not), or is a forward-port PR, fwbot should
    just feedback that it won't do it
    """
    prod, _ = make_basic(env, config, make_repo)
    reviewer = config['role_reviewer']['token']
    branch_c = env['runbot_merge.branch'].search([('name', '=', 'c')])
    bot_name = env['runbot_merge.project'].search([]).fp_github_name
    with prod:
        [c] = prod.make_commits('a', Commit('c', tree={'0': '0'}), ref='heads/abranch')
        pr1 = prod.make_pr(target='a', head='abranch')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr1.post_comment('hansen r+', reviewer)
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()

    p1, p2 = env['runbot_merge.pull_requests'].search([], order='number')
    assert p1.limit_id == p2.limit_id == branch_c, "check that limit is correctly set"
    pr2 = prod.get_pr(p2.number)
    with prod:
        pr1.post_comment(bot_name + ' up to b', reviewer)
        pr2.post_comment(bot_name + ' up to b', reviewer)
    env.run_crons()

    assert p1.limit_id == p2.limit_id == branch_c, \
        "check that limit was not updated"
    assert pr1.comments == [
        (users['reviewer'], "hansen r+"),
        (users['reviewer'], bot_name + ' up to b'),
        (bot_name, "Sorry, forward-port limit can only be set before the PR is merged."),
    ]
    assert pr2.comments == [
        (users['user'], """\
This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
"""),
        (users['reviewer'], bot_name + ' up to b'),
        (bot_name, "Sorry, forward-port limit can only be set on an origin PR"
                   " (%s here) before it's merged and forward-ported." % p1.display_name
         ),
    ]
