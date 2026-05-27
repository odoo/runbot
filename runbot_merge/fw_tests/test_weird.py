# -*- coding: utf-8 -*-
from datetime import datetime, timedelta, timezone

import pytest

from utils import seen, Commit, to_pr, make_basic, prevent_unstaging, pr_page, matches


def test_no_token(env, config, make_repo):
    """ if there's no token on the repo, nothing should break though should
    log
    """
    # create project configured with remotes on the repo but no token
    prod, _ = make_basic(env, config, make_repo, fp_token=False, fp_remote=True, statuses='default')

    with prod:
        prod.make_commits(
            'a', Commit('c0', tree={'a': '0'}), ref='heads/abranch'
        )
        pr = prod.make_pr(target='a', head='abranch')
        prod.post_status(pr.head, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success')

    # wanted to use capfd, however it's not compatible with the subprocess
    # being created beforehand and server() depending on capfd() would remove
    # all its output from the normal pytest capture (dumped on test failure)
    #
    # so I'd really have to hand-roll the entire thing by having server()
    # pipe stdout/stderr to temp files, yield those temp files, and have the
    # tests mess around with reading those files, and finally have the server
    # dump the file contents back to the test runner's stdout/stderr on
    # fixture teardown...
    env.run_crons()
    assert len(env['runbot_merge.pull_requests'].search([], order='number')) == 1,\
        "should not have created forward port"

def test_remove_token(env, config, make_repo):
    prod, _ = make_basic(env, config, make_repo, statuses='default', fp_token=False)

    with prod:
        prod.make_commits(
            'a', Commit('c0', tree={'a': '0'}), ref='heads/abranch'
        )
        pr = prod.make_pr(target='a', head='abranch')
        prod.post_status(pr.head, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success')

    env.run_crons()
    assert len(env['runbot_merge.pull_requests'].search([], order='number')) == 1,\
        "should not have created forward port"

def test_no_target(env, config, make_repo):
    prod, _ = make_basic(env, config, make_repo, fp_remote=False, statuses='default')

    with prod:
        prod.make_commits(
            'a', Commit('c0', tree={'a': '0'}), ref='heads/abranch'
        )
        pr = prod.make_pr(target='a', head='abranch')
        prod.post_status(pr.head, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success')

    env.run_crons()
    assert len(env['runbot_merge.pull_requests'].search([], order='number')) == 1,\
        "should not have created forward port"

def test_failed_staging(env, config, make_repo):
    prod, _ = make_basic(env, config, make_repo, statuses='default')

    reviewer = config['role_reviewer']['token']
    with prod:
        prod.make_commits('a', Commit('c', tree={'a': '0'}), ref='heads/abranch')
        pr1 = prod.make_pr(target='a', head='abranch')
        prod.post_status(pr1.head, 'success')
        pr1.post_comment('hansen r+', reviewer)
    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    pr1_id, pr2_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert pr2_id.parent_id == pr2_id.source_id == pr1_id
    with prod:
        prod.post_status(pr2_id.head, 'success')
    env.run_crons()

    _pr1_id, _pr2_id, pr3_id = env['runbot_merge.pull_requests'].search([], order='number')
    pr3 = prod.get_pr(pr3_id.number)
    with prod:
        prod.post_status(pr3_id.head, 'success')
        pr3.post_comment('hansen r+', reviewer)
    env.run_crons()

    prod.commit('staging.c')

    with prod:
        prod.post_status('staging.b', 'success')
        prod.post_status('staging.c', 'failure')
    env.run_crons()

    pr3_head = env['runbot_merge.commit'].search([('sha', '=', pr3_id.head)])
    assert pr3_head

    # send a new status to the PR, as if somebody had rebuilt it or something
    with prod:
        pr3.post_comment('hansen retry', reviewer)
        prod.post_status(pr3_id.head, 'success')
    assert pr3_head.to_check, "check that the commit was updated as to process"
    env.run_crons()
    assert not pr3_head.to_check, "check that the commit was processed"
    assert pr3_id.state == 'ready'
    assert pr3_id.staging_id

def test_fw_retry(env, config, make_repo, users):
    prod, _ = make_basic(env, config, make_repo, statuses='default')
    other_token = config['role_other']['token']
    fork = prod.fork(token=other_token)
    with prod, fork:
        fork.make_commits(prod.commit('a').id, Commit('c', tree={'a': '0'}), ref='heads/abranch')
        pr1 = prod.make_pr(
            title="whatever",
            target='a',
            head=f'{fork.owner}:abranch',
            token=other_token,
        )
        prod.post_status(pr1.head, 'success')
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    other_partner = env['res.partner'].search([('github_login', '=', users['other'])])
    assert len(other_partner) == 1
    other_partner.email = "foo@example.com"

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    _pr1_id, pr2_id = env['runbot_merge.pull_requests'].search([], order='number')
    pr2 = prod.get_pr(pr2_id.number)
    with prod:
        prod.post_status(pr2_id.head, 'success')
        pr2.post_comment('hansen r+', other_token)
    env.run_crons()
    assert not pr2_id.blocked

    with prod:
        prod.post_status('staging.b', 'failure')
    env.run_crons()

    assert pr2_id.error
    with prod:
        pr2.post_comment('hansen r+', other_token)
    env.run_crons()
    assert pr2_id.state == 'error'
    with prod:
        pr2.post_comment('hansen retry', other_token)
    env.run_crons()
    assert pr2_id.state == 'ready'

    assert pr2.comments == [
        seen(env, pr2, users),
        (users['user'], "This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.\n\nMore info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port\n"),
        (users['other'], 'hansen r+'),
        (users['user'], "@{other} @{reviewer} staging failed: default".format_map(users)),

        (users['other'], 'hansen r+'),
        (users['user'], "This PR is already reviewed, it's in error, you might want to `retry` it instead (if you have already confirmed the error is not legitimate)."),

        (users['other'], 'hansen retry'),
    ]

class TestNotAllBranches:
    """ Check that forward-ports don't behave completely insanely when not all
    branches are supported on all repositories.

    repo A branches a -> b -> c
    a0 -> a1 -> a2       branch a
          `-> a11 -> a22 branch b
              `-> a111   branch c
    repo B branches a ->      c
    b0 -> b1 -> b2       branch a
          |
          `-> b000       branch c
    """
    @pytest.fixture
    def repos(self, env, config, make_repo, setreviewers):
        a = make_repo('A')
        with a:
            _, a_, _ = a.make_commits(
                None,
                Commit('a0', tree={'a': '0'}),
                Commit('a1', tree={'a': '1'}),
                Commit('a2', tree={'a': '2'}),
                ref='heads/a'
            )
            b_, _ = a.make_commits(
                a_,
                Commit('a11', tree={'b': '11'}),
                Commit('a22', tree={'b': '22'}),
                ref='heads/b'
            )
            a.make_commits(b_, Commit('a111', tree={'c': '111'}), ref='heads/c')
        a_dev = a.fork()
        b = make_repo('B')
        with b:
            _, _a, _ = b.make_commits(
                None,
                Commit('b0', tree={'a': 'x'}),
                Commit('b1', tree={'a': 'y'}),
                Commit('b2', tree={'a': 'z'}),
                ref='heads/a'
            )
            b.make_commits(_a, Commit('b000', tree={'c': 'x'}), ref='heads/c')
        b_dev = b.fork()

        project = env['runbot_merge.project'].create({
            'name': 'proj',
            'github_token': config['github']['token'],
            'github_prefix': 'hansen',
            'github_name': config['github']['name'],
            'github_email': "foo@example.org",
            'fp_github_token': config['github']['token'],
            'fp_github_name': 'herbert',
            'branch_ids': [
                (0, 0, {'name': 'a', 'sequence': 2}),
                (0, 0, {'name': 'b', 'sequence': 1}),
                (0, 0, {'name': 'c', 'sequence': 0}),
            ]
        })
        repo_a = env['runbot_merge.repository'].create({
            'project_id': project.id,
            'name': a.name,
            'required_statuses': 'default',
            'fp_remote_target': a_dev.name,
        })
        repo_b = env['runbot_merge.repository'].create({
            'project_id': project.id,
            'name': b.name,
            'required_statuses': 'default',
            'fp_remote_target': b_dev.name,
            'branch_filter': '[("name", "in", ["a", "c"])]',
        })
        setreviewers(repo_a, repo_b)
        env['runbot_merge.events_sources'].create([{'repository': a.name}, {'repository': b.name}])
        return project, a, a_dev, b, b_dev

    def test_single_first(self, env, repos, config):
        """ A merge in A.a should be forward-ported to A.b and A.c
        """
        _project, a, a_dev, b, _ = repos
        with a, a_dev:
            [c] = a_dev.make_commits(a.commit('a').id, Commit('pr', tree={'pr': '1'}), ref='heads/change')
            pr = a.make_pr(target='a', title="a pr", head=a_dev.owner + ':change')
            a.post_status(c, 'success')
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
        p = to_pr(env, pr)
        env.run_crons()
        assert p.staging_id
        with a, b:
            a.post_status('staging.a', 'success')
            b.post_status('staging.a', 'success')
        env.run_crons()

        a_head = a.commit('a')
        assert a_head.message.startswith('pr\n\n')
        assert a.read_tree(a_head) == {'a': '2', 'pr': '1'}

        _pr0, pr1 = env['runbot_merge.pull_requests'].search([], order='number')
        with a:
            a.post_status(pr1.head, 'success')
        env.run_crons()

        pr0, pr1, pr2 = env['runbot_merge.pull_requests'].search([], order='number')
        with a:
            a.post_status(pr2.head, 'success')
            a.get_pr(pr2.number).post_comment(
                'hansen r+',
                config['role_reviewer']['token'])
        env.run_crons()
        assert pr1.staging_id
        assert pr2.staging_id
        with a, b:
            a.post_status('staging.b', 'success')
            a.post_status('staging.c', 'success')
            b.post_status('staging.c', 'success')
        env.run_crons()

        assert pr0.state == 'merged'
        assert pr1.state == 'merged'
        assert pr2.state == 'merged'
        assert a.read_tree(a.commit('b')) == {'a': '1', 'b': '22',             'pr': '1'}
        assert a.read_tree(a.commit('c')) == {'a': '1', 'b': '11', 'c': '111', 'pr': '1'}

    def test_single_second(self, env, repos, config):
        """ A merge in B.a should "skip ahead" to B.c
        """
        _project, a, _, b, b_dev = repos
        with b, b_dev:
            [c] = b_dev.make_commits(b.commit('a').id, Commit('pr', tree={'pr': '1'}), ref='heads/change')
            pr = b.make_pr(target='a', title="a pr", head=b_dev.owner + ':change')
            b.post_status(c, 'success')
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with a, b:
            a.post_status('staging.a', 'success')
            b.post_status('staging.a', 'success')
        env.run_crons()

        assert b.read_tree(b.commit('a')) == {'a': 'z', 'pr': '1'}

        pr0, pr1 = env['runbot_merge.pull_requests'].search([], order='number')
        with b:
            b.post_status(pr1.head, 'success')
            b.get_pr(pr1.number).post_comment(
                'hansen r+',
                config['role_reviewer']['token'])
        env.run_crons()
        with a, b:
            a.post_status('staging.c', 'success')
            b.post_status('staging.c', 'success')
        env.run_crons()

        assert pr0.state == 'merged'
        assert pr1.state == 'merged'
        assert b.read_tree(b.commit('c')) == {'a': 'y', 'c': 'x', 'pr': '1'}

    def test_both_first(self, env, repos, config, users):
        """ A merge in A.a, B.a should... not be forward-ported at all?
        """
        _project, a, a_dev, b, b_dev = repos
        with a, a_dev:
            [c_a] = a_dev.make_commits(a.commit('a').id, Commit('pr a', tree={'pr': 'a'}), ref='heads/change')
            pr_a = a.make_pr(target='a', title='a pr', head=a_dev.owner + ':change')
            a.post_status(c_a, 'success')
            pr_a.post_comment('hansen r+', config['role_reviewer']['token'])
        with b, b_dev:
            [c_b] = b_dev.make_commits(b.commit('a').id, Commit('pr b', tree={'pr': 'b'}), ref='heads/change')
            pr_b = b.make_pr(target='a', title='b pr', head=b_dev.owner + ':change')
            b.post_status(c_b, 'success')
            pr_b.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with a, b:
            for repo in a, b:
                repo.post_status('staging.a', 'success')
        env.run_crons()

        pr_a_id = to_pr(env, pr_a)
        pr_b_id = to_pr(env, pr_b)
        assert pr_a_id.state == pr_b_id.state == 'merged'
        assert env['runbot_merge.pull_requests'].search([]) == pr_a_id | pr_b_id
        # should have refused to create a forward port because the PRs have
        # different next target
        assert pr_a.comments == [
            (users['reviewer'], 'hansen r+'),
            seen(env, pr_a, users),
            (users['user'], "@%s @%s this pull request can not be forward-ported:"
                            " next branch is 'b' but linked pull request %s "
                            "has a next branch 'c'." % (
                users['user'], users['reviewer'], pr_b_id.display_name,
            )),
        ]
        assert pr_b.comments == [
            (users['reviewer'], 'hansen r+'),
            seen(env, pr_b, users),
            (users['user'], "@%s @%s this pull request can not be forward-ported:"
                            " next branch is 'c' but linked pull request %s "
                            "has a next branch 'b'." % (
                users['user'], users['reviewer'], pr_a_id.display_name,
            )),
        ]

def test_new_intermediate_branch(env, config, make_repo):
    """ In the case of a freeze / release a new intermediate branch appears in
    the sequence. New or ongoing forward ports should pick it up just fine (as
    the "next target" is decided when a PR is ported forward) however this is
    an issue for existing yet-to-be-merged sequences e.g. given the branches
    1.0, 2.0 and master, if a branch 3.0 is forked off from master and inserted
    before it, we need to create a new *intermediate* forward port PR
    """
    prod, _ = make_basic(env, config, make_repo, statuses='default')
    prod2, _ = make_basic(env, config, make_repo, statuses='default')
    project = env['runbot_merge.project'].search([])
    assert len(project.repo_ids) == 2

    original_c_tree = prod.read_tree(prod.commit('c'))
    prs = []
    with prod, prod2:
        for i in ['0', '1']:
            prod.make_commits('a', Commit(i, tree={i:i}), ref='heads/branch%s' % i)
            pr = prod.make_pr(target='a', head='branch%s' % i)
            prs.append(pr)
            prod.post_status(pr.head, 'success')
            pr.post_comment('hansen r+', config['role_reviewer']['token'])

        # also add a PR targeting b forward-ported to c, in order to check
        # for an insertion right after the source, as well as have linked PRs in
        # two different repos
        prod.make_commits('b', Commit('x', tree={'x': 'x'}), ref='heads/branchx')
        prod2.make_commits('b', Commit('x2', tree={'x': 'x2'}), ref='heads/branchx')
        prx = prod.make_pr(target='b', head='branchx')
        prx2 = prod2.make_pr(target='b', head='branchx')
        prod.post_status(prx.head, 'success')
        prod2.post_status(prx2.head, 'success')
        prx.post_comment('hansen r+', config['role_reviewer']['token'])
        prx2.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod, prod2:
        for r in [prod, prod2]:
            r.post_status('staging.a', 'success')
            r.post_status('staging.b', 'success')
    env.run_crons()

    # should have merged pr1, pr2 and prx and created their forward ports, now
    # validate pr0's FP so the c-targeted FP is created
    PRs = env['runbot_merge.pull_requests']
    pr0_id = to_pr(env, prs[0])
    pr0_fp_id = PRs.search([
        ('source_id', '=', pr0_id.id),
    ])
    assert pr0_fp_id
    assert pr0_fp_id.target.name == 'b'
    with prod:
        prod.post_status(pr0_fp_id.head, 'success')
    env.run_crons()
    assert pr0_fp_id.state == 'validated'
    original0 = PRs.search([('parent_id', '=', pr0_fp_id.id)])
    assert original0, "Could not find FP of PR0 to C"
    assert original0.target.name == 'c'

    pr1_id = to_pr(env, prs[1])
    pr1_fp_id = PRs.search([('source_id', '=', pr1_id.id)])
    assert pr1_fp_id.target.name == 'b'

    # also check prx's fp
    prx_id = to_pr(env, prx)
    prx2_id = to_pr(env, prx2)
    assert prx_id.label == prx2_id.label

    prx_fp_id = PRs.search([('source_id', '=', prx_id.id)])
    assert prx_fp_id
    assert prx_fp_id.target.name == 'c'
    prx2_fp_id = PRs.search([('source_id', '=', prx2_id.id)])
    assert prx2_fp_id
    assert prx2_fp_id.target.name == 'c'
    assert prx_fp_id.label == prx2_fp_id.label,\
        "ensure labels of PRs of same batch are the same"

    # NOTE: the branch must be created on git(hub) first, probably
    # create new branch forked from the "current master" (c)
    c = prod.commit('c').id
    with prod:
        prod.make_ref('heads/new', c)
    c2 = prod2.commit('c').id
    with prod2:
        prod2.make_ref('heads/new', c2)
    currents = {branch.name: branch.id for branch in project.branch_ids}
    # insert a branch between "b" and "c"
    project.write({
        'branch_ids': [
            (1, currents['a'], {'sequence': 3}),
            (1, currents['b'], {'sequence': 2, 'active': False}),
            (1, currents['c'], {'sequence': 0})
        ]
    })
    env.run_crons()
    project.write({
        'branch_ids': [
            (0, False, {'name': 'new', 'sequence': 1}),
        ]
    })
    env.run_crons()

    assert pr0_fp_id.state == 'validated'
    # created an intermediate PR for 0 and x
    desc0 = PRs.search([('source_id', '=', pr0_id.id)])
    new0 = desc0 - pr0_fp_id - original0
    assert len(new0) == 1
    assert new0.parent_id == pr0_fp_id
    assert new0.target.name == 'new'
    assert original0.parent_id == new0

    descx = PRs.search([('source_id', '=', prx_id.id)])
    newx = descx - prx_fp_id
    assert len(newx) == 1
    assert newx.parent_id == prx_id
    assert newx.target.name == 'new'
    assert prx_fp_id.parent_id == newx

    descx2 = PRs.search([('source_id', '=', prx2_id.id)])
    newx2 = descx2 - prx2_fp_id
    assert len(newx2) == 1
    assert newx2.parent_id == prx2_id
    assert newx2.target.name == 'new'
    assert prx2_fp_id.parent_id == newx2

    assert newx.label == newx2.label

    # created followups for 1
    # earliest followup is followup from deactivating a branch, creates fp in
    # n+1 = c (from b), then inserting a new branch between b and c should
    # create a bridge forward port
    _, pr1_c, pr1_new = PRs.search([('source_id', '=', pr1_id.id)], order='number')
    assert pr1_c.target.name == 'c'
    assert pr1_new.target.name == 'new'
    assert pr1_c.parent_id == pr1_new
    assert pr1_new.parent_id == pr1_fp_id

    # ci on pr1/pr2 fp to b
    sources = [to_pr(env, pr).id for pr in prs]
    sources.append(prx_id.id)
    sources.append(prx2_id.id)

    def get_repo(pr):
        if pr.repository.name == prod.name:
            return prod
        return prod2
    # CI all the forward port PRs (shouldn't hurt to re-ci the forward port of
    # prs[0] to b aka pr0_fp_id
    fps = PRs.search([('source_id', 'in', sources), ('target.name', '=', ['new', 'c'])])
    with prod, prod2:
        for fp in fps:
            repo = get_repo(fp)
            repo.post_status(fp.head, 'success')
    env.run_crons()
    # now fps should be the last PR of each sequence, and thus r+-able (via
    # fwbot so preceding PR is also r+'d)
    with prod, prod2:
        for pr in fps.filtered(lambda p: p.target.name == 'c'):
            get_repo(pr).get_pr(pr.number).post_comment(
                'hansen r+',
                config['role_reviewer']['token'])
    assert all(p.state == 'merged' for p in PRs.browse(sources)),\
        "all sources should be merged"
    assert all(p.state == 'ready' for p in PRs.search([('source_id', '!=', False), ('target.name', '!=', 'b')])), \
        "All PRs except sources and prs on disabled branch should be ready"
    env.run_crons()

    assert len(env['runbot_merge.stagings'].search([])) == 2,\
        "enabled branches should have been staged"
    with prod, prod2:
        for target in ['new', 'c']:
            prod.post_status(f'staging.{target}', 'success')
            prod2.post_status(f'staging.{target}', 'success')
    env.run_crons()
    assert all(p.state == 'merged' for p in PRs.search([('target.name', '!=', 'b')])), \
        "All PRs except disabled branch should be merged now"

    assert prod.read_tree(prod.commit('c')) == {
        **original_c_tree,
        '0': '0', '1': '1', # updates from PRs
        'x': 'x',
    }, "check that C got all the updates"
    assert prod.read_tree(prod.commit('new')) == {
        **original_c_tree,
        '0': '0', '1': '1', # updates from PRs
        'x': 'x',
    }, "check that new got all the updates (should be in the same state as c really)"

def test_author_can_close_via_fwbot(env, config, make_repo):
    prod, _ = make_basic(env, config, make_repo, statuses='default')
    other_user = config['role_other']
    other_token = other_user['token']
    other = prod.fork(token=other_token)

    with prod, other:
        [c] = other.make_commits(prod.commit('a').id, Commit('c', tree={'0': '0'}), ref='heads/change')
        pr = prod.make_pr(
            target='a', title='my change',
            head=other_user['user'] + ':change',
            token=other_token
        )
        # should be able to close and open own PR
        pr.close(other_token)
        pr.open(other_token)
        prod.post_status(c, 'success')
        pr.post_comment('hansen close', other_token)
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    assert pr.state == 'open'

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    pr0_id, pr1_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert pr0_id.number == pr.number
    pr1 = prod.get_pr(pr1_id.number)
    # `other` can't close fw PR directly, because that requires triage (and even
    # write depending on account type) access to the repo, which an external
    # contributor probably does not have
    with prod, pytest.raises(Exception):
        pr1.close(other_token)
    # use can close via fwbot
    with prod:
        pr1.post_comment('hansen close', other_token)
    env.run_crons()
    assert pr1.state == 'closed'
    assert pr1_id.state == 'closed'

def test_skip_ci_all(env, config, make_repo):
    prod, _ = make_basic(env, config, make_repo, statuses='default')

    with prod:
        prod.make_commits('a', Commit('x', tree={'x': '0'}), ref='heads/change')
        pr = prod.make_pr(target='a', head='change')
        prod.post_status(pr.head, 'success')
        pr.post_comment('hansen fw=skipci', config['role_reviewer']['token'])
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    assert to_pr(env, pr).batch_id.fw_policy == 'skipci'

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    # run cron a few more times for the fps
    env.run_crons()
    env.run_crons()
    env.run_crons()

    pr0_id, pr1_id, pr2_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert pr1_id.state == 'opened'
    assert pr1_id.source_id == pr0_id
    assert pr2_id.state == 'opened'
    assert pr2_id.source_id == pr0_id

def test_skip_ci_next(env, config, make_repo):
    prod, _ = make_basic(env, config, make_repo, statuses='default')

    with prod:
        prod.make_commits('a', Commit('x', tree={'x': '0'}), ref='heads/change')
        pr = prod.make_pr(target='a', head='change')
        prod.post_status(pr.head, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    pr0_id, pr1_id = env['runbot_merge.pull_requests'].search([], order='number')
    with prod:
        prod.get_pr(pr1_id.number).post_comment(
            'hansen fw=skipci',
            config['role_reviewer']['token']
        )
    assert pr0_id.batch_id.fw_policy == 'skipci'
    env.run_crons()

    _, _, pr2_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert pr1_id.state == 'opened'
    assert pr2_id.state == 'opened'

def test_retarget_after_freeze(env, config, make_repo, users):
    """Turns out it was possible to trip the forwardbot if you're a bit of a
    dick: the forward port cron was not resilient to forward port failure in
    case of filling in new branches (forward ports existing across a branch
    insertion so the fwbot would have to "fill in" for the new branch).

    But it turns out causing such failure is possible by e.g. regargeting the
    latter port. In that case the reinsertion task should just do nothing, and
    the retargeted PR should be forward-ported normally once merged.
    """
    prod, _ = make_basic(env, config, make_repo, statuses='default')
    project = env['runbot_merge.project'].search([])
    with prod:
        [c] = prod.make_commits('b', Commit('thing', tree={'x': '1'}), ref='heads/mypr')
        pr = prod.make_pr(target='b', head='mypr')
        prod.post_status(c, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    original_pr_id = to_pr(env, pr)
    assert original_pr_id.state == 'ready'
    assert original_pr_id.staging_id

    with prod:
        prod.post_status('staging.b', 'success')
    env.run_crons()
    # should have created a pr targeted to C
    port_id = env['runbot_merge.pull_requests'].search([('state', 'not in', ('merged', 'closed'))])
    assert len(port_id) == 1
    assert port_id.target.name == 'c'
    assert port_id.source_id == original_pr_id
    assert port_id.parent_id == original_pr_id

    branch_c, branch_b, branch_a = branches_before = project.branch_ids
    assert [branch_a.name, branch_b.name, branch_c.name] == ['a', 'b', 'c']
    # create branch so cron runs correctly
    with prod: prod.make_ref('heads/bprime', prod.get_ref('c'))
    project.write({
        'branch_ids': [
            (1, branch_c.id, {'sequence': 1}),
            (0, 0, {'name': 'bprime', 'sequence': 2}),
            (1, branch_b.id, {'sequence': 3}),
            (1, branch_a.id, {'sequence': 4}),
        ]
    })
    new_branch = project.branch_ids - branches_before
    assert new_branch.name == 'bprime'

    # should have added a job for the new fp
    job = env['forwardport.batches'].search([])
    assert job

    # fuck up yo life: retarget the existing FP PR to the new branch
    port_pr = prod.get_pr(port_id.number)
    with prod:
        port_pr.base = 'bprime'
    assert port_id.target == new_branch

    env.run_crons(None)
    assert not job.exists(), "job should have succeeded and apoptosed"

    # since the PR was "already forward-ported" to the new branch it should not
    # be touched
    assert env['runbot_merge.pull_requests'].search([('state', 'not in', ('merged', 'closed'))]) == port_id

    # merge the retargered PR
    with prod:
        prod.post_status(port_pr.head, 'success')
        port_pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.bprime', 'success')
    env.run_crons()

    # #2 batch 6 (???)
    assert port_id.state == 'merged'

    new_pr_id = env['runbot_merge.pull_requests'].search([('state', 'not in', ('merged', 'closed'))])
    assert len(new_pr_id) == 1
    assert new_pr_id.parent_id == port_id
    assert new_pr_id.target == branch_c

def test_approve_draft(env, config, make_repo, users):
    prod, _ = make_basic(env, config, make_repo, statuses='default')

    with prod:
        prod.make_commits('a', Commit('x', tree={'x': '0'}), ref='heads/change')
        pr = prod.make_pr(target='a', head='change', draft=True)
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    pr_id = to_pr(env, pr)
    assert pr_id.state == 'opened'
    assert pr.comments == [
        (users['reviewer'], 'hansen r+'),
        seen(env, pr, users),
        (users['user'], f"@{users['reviewer']} draft PRs can not be approved."),
    ]

    with prod:
        pr.draft = False
    assert pr.draft is False
    with prod:
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    assert pr_id.state == 'approved'

def test_freeze(env, config, make_repo, users):
    """Freeze:

    - should not forward-port the freeze PRs themselves
    - unmerged forward ports need to be backfilled
    - if the tip of the forward port is approved, the backfilled forward port
      should also be
    """
    prod, _ = make_basic(env, config, make_repo, statuses='default')
    project = env['runbot_merge.project'].search([])


    # branches here are "a" (older), "b", and "c" (master)
    with prod:
        [root, _] = prod.make_commits(
            None,
            Commit('base', tree={'version': '', 'f': '0'}),
            Commit('release 1.0', tree={'version': '1.0'}),
            ref='heads/b'
        )
        prod.make_commits(root, Commit('other', tree={'f': '1'}), ref='heads/c')

    # region PR which is forward ported but the FPs are not merged (they are approved)
    with prod:
        prod.make_commits("a", Commit("stuff", tree={'x': '0'}), ref="heads/abranch")
        p = prod.make_pr(target='a', head='abranch')
        p.post_comment("hansen r+ fw=skipci", config['role_reviewer']['token'])
        prod.post_status('abranch', 'success')
    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()
    pr_a_id, pr_b_id, pr_c_id = pr_ids = env['runbot_merge.pull_requests'].search([], order='number')
    assert len(pr_ids) == 3, \
        "should have created two forward ports, one in b and one in c (/ master)"
    # endregion

    with prod:
        prod.make_commits(
            'c',
            Commit('Release 1.1', tree={'version': '1.1'}),
            ref='heads/release-1.1'
        )
        release = prod.make_pr(target='c', head='release-1.1')
    env.run_crons()

    # approve pr_c_id but don't actually merge it before freezing
    with prod:
        prod.post_status(pr_b_id.head, 'success')
        prod.post_status(pr_c_id.head, 'success')
        prod.get_pr(pr_c_id.number).post_comment('hansen r+', config['role_reviewer']['token'])
    # review comment should be handled eagerly
    assert pr_b_id.reviewed_by
    assert pr_c_id.reviewed_by

    w = project.action_prepare_freeze()
    assert w['res_model'] == 'runbot_merge.project.freeze'
    w_id = env[w['res_model']].browse([w['res_id']])
    assert w_id.release_pr_ids.repository_id.name == prod.name
    release_id = to_pr(env, release)
    w_id.release_pr_ids.pr_id = release_id.id

    assert not w_id.errors
    w_id.action_freeze()

    assert release_id.state == 'merged'

    assert project.branch_ids.mapped('name') == ['c', 'post-b', 'b', 'a']
    # the tip branch should have staging disabled
    tip = project.branch_ids.filtered(lambda b: b.name == 'c')
    assert tip.staging_enabled == False

    # re-enable staging
    tip.staging_enabled = True
    # re-enable forward-port cron after freeze
    _, cron_id = env['ir.model.data'].check_object_reference('runbot_merge', 'port_forward', context={'active_test': False})
    env['ir.cron'].browse([cron_id]).active = True
    env.run_crons('runbot_merge.port_forward')
    assert not env['runbot_merge.pull_requests'].search([
        ('source_id', '=', release_id.id),
    ]), "the release PRs should not be forward-ported"

    assert env['runbot_merge.stagings'].search_count([]) == 2,\
        "b and c forward ports should be staged since they were ready before freeze"

    # an intermediate PR should have been created
    pr_inserted = env['runbot_merge.pull_requests'].search([
        ('source_id', '=', pr_a_id.id),
        ('target.name', '=', 'post-b'),
    ])
    assert pr_inserted, "an intermediate PR should have been reinsered in the sequence"
    assert pr_c_id.parent_id == pr_inserted
    assert pr_inserted.parent_id == pr_b_id

    assert pr_inserted.reviewed_by == pr_c_id.reviewed_by,\
        "review state should have been copied over from c (master)"
    with prod:
        prod.post_status(pr_inserted.head, 'success')
        prod.post_status('staging.b', 'success')
        prod.post_status('staging.c', 'success')
    env.run_crons()
    with prod:
        prod.post_status('staging.post-b', 'success')
    env.run_crons()

    assert env['runbot_merge.pull_requests'].search_count([('state', '=', 'merged')]) \
        == len(['release', 'initial', 'fw-b', 'fw-post-b', 'fw-c'])


@pytest.mark.expect_log_errors(reason="missing / invalid head causes an error to be logged")
def test_missing_magic_ref(env, config, make_repo):
    """There are cases where github fails to create / publish or fails to update
    the magic refs in refs/pull/*.

    In that case, pulling from the regular remote does not bring in the contents
    of the PR we're trying to forward port, and the forward porting process
    fails.

    Emulate this behaviour by updating the PR with a commit which lives in the
    repo but has no ref.
    """
    prod, _ = make_basic(env, config, make_repo, statuses='default')
    a_head = prod.commit('refs/heads/a')
    with prod:
        [c] = prod.make_commits(a_head.id, Commit('x', tree={'x': '0'}), ref='heads/change')
        pr = prod.make_pr(target='a', head='change')
        prod.post_status(c, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    # create variant of pr head in fork, update PR with that commit as head so
    # it's not found after a fetch, simulating an outdated or missing magic ref
    pr_id = to_pr(env, pr)
    assert pr_id.staging_id

    with prevent_unstaging(pr_id.staging_id):
        pr_id.head = '0'*40
    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    assert not pr_id.staging_id
    assert pr_id.state == 'merged'

    # check that the fw failed
    assert not env['runbot_merge.pull_requests'].search([('source_id', '=', pr_id.id)]),\
        "forward port should not have been created"
    # check that the batch is still here and targeted for the future
    req = env['forwardport.batches'].search([])
    assert len(req) == 1
    assert req.retry_after > datetime.now(timezone.utc)\
        .replace(tzinfo=None).isoformat(" ", "seconds")

    # check notifications
    assert not req.disabled
    env['res.users'].search([]).notification_type = 'inbox'
    req.retry_after = datetime.now().isoformat(" ", "seconds")
    req.sequence = 23
    env.run_crons(None)
    assert req.sequence == 24
    assert req.disabled
    notifications = env['mail.notification'].search([])
    message_id = notifications.mail_message_id
    assert message_id.res_id == req.id
    assert message_id.body == "<p>Cannot process, disabling.</p>"

    # re-enable
    req.sequence = 0
    req.retry_after = datetime.now().isoformat(" ", "seconds")

    # add a real commit
    with prod:
        [c2] = prod.make_commits(a_head.id, Commit('y', tree={'x': '0'}))
    assert c2 != c
    pr_id.head = c2
    env.run_crons(None)

    fp_id = env['runbot_merge.pull_requests'].search([('source_id', '=', pr_id.id)])
    assert fp_id
    # the cherrypick process fetches the commits on the PR in order to know
    # what they are (rather than e.g. diff the HEAD it branch with the target)
    # as a result it doesn't forwardport our fake, we'd have to reset the PR's
    # branch for that to happen

def test_disable_branch_with_batches(env, config, make_repo, users):
    """We want to avoid losing pull requests, so when deactivating a branch,
    if there are *forward port* batches targeting that branch which have not
    been forward ported yet port them over, as if their source had been merged
    after the branch was disabled (thus skipped over)
    """
    repo, fork = make_basic(env, config, make_repo, statuses="default")
    proj = env['runbot_merge.project'].search([])
    branch_b = env['runbot_merge.branch'].search([('name', '=', 'b')])
    assert branch_b

    # region repo2 creation & setup
    repo2 = make_repo('proj2')
    with repo2:
        [a, b, c] = repo2.make_commits(
            None,
            Commit("a", tree={"f": "a"}),
            Commit("b", tree={"g": "b"}),
            Commit("c", tree={"h": "c"}),
        )
        repo2.make_ref("heads/a", a)
        repo2.make_ref("heads/b", b)
        repo2.make_ref("heads/c", c)
    fork2 = repo2.fork()
    repo2_id = env['runbot_merge.repository'].create({
        "project_id": proj.id,
        "name": repo2.name,
        "required_statuses": "default",
        "fp_remote_target": fork2.name,
    })
    env['runbot_merge.events_sources'].create({'repository': repo2.name})
    env['res.partner'].search([
        ('github_login', '=', config['role_reviewer']['user'])
    ]).write({
        'review_rights': [(0, 0, {'repository_id': repo2_id.id, 'review': True})]
    })
    env['res.partner'].search([
        ('github_login', '=', config['role_self_reviewer']['user'])
    ]).write({
        'review_rights': [(0, 0, {'repository_id': repo2_id.id, 'self_review': True})]
    })
    # endregion

    # region set up forward ported batches
    with repo, fork, repo2, fork2:
        fork.make_commits(repo.commit("a").id, Commit("x", tree={"x": "1"}), ref="heads/x")
        pr1_a = repo.make_pr(title="X", target="a", head=f"{fork.owner}:x")
        pr1_a.post_comment("hansen r+", config['role_reviewer']['token'])
        repo.post_status(pr1_a.head, "success")

        fork2.make_commits(repo2.commit("a").id, Commit("x", tree={"x": "1"}), ref="heads/x")
        pr2_a = repo2.make_pr(title="X", target="a", head=f"{fork2.owner}:x")
        pr2_a.post_comment("hansen r+", config['role_reviewer']['token'])
        repo2.post_status(pr2_a.head, "success")

        fork.make_commits(repo.commit("a").id, Commit("y", tree={"y": "1"}), ref="heads/y")
        pr3_a = repo.make_pr(title="Y", target="a", head=f"{fork.owner}:y")
        pr3_a.post_comment("hansen r+", config['role_reviewer']['token'])
        repo.post_status(pr3_a.head, 'success')
    # remove just pr2 from the forward ports (maybe?)
    pr2_a_id = to_pr(env, pr2_a)
    pr2_a_id.limit_id = branch_b.id
    env.run_crons()
    assert pr2_a_id.limit_id == branch_b
    # endregion

    with repo, repo2:
        repo.post_status('staging.a', 'success')
        repo2.post_status('staging.a', 'success')
    env.run_crons()

    PullRequests = env['runbot_merge.pull_requests']
    pr1_b_id = PullRequests.search([('parent_id', '=', to_pr(env, pr1_a).id)])
    pr2_b_id = PullRequests.search([('parent_id', '=', pr2_a_id.id)])
    pr3_b_id = PullRequests.search([('parent_id', '=', to_pr(env, pr3_a).id)])
    assert pr1_b_id.parent_id
    assert pr1_b_id.state == 'opened'
    assert pr2_b_id.parent_id
    assert pr2_b_id.state == 'opened'
    assert pr3_b_id.parent_id
    assert pr3_b_id.state == 'opened'
    # detach pr3 (?)
    pr3_b_id.write({'parent_id': False, 'detach_reason': 'because'})

    b_id = proj.branch_ids.filtered(lambda b: b.name == 'b')
    proj.write({
        'branch_ids': [(1, b_id.id, {'active': False})]
    })
    env.run_crons()
    assert not b_id.active
    # pr1_a, pr1_b, pr1_c, pr2_a, pr2_b, pr3_a, pr3_b, pr3_c
    assert PullRequests.search_count([]) == 8, "should have ported pr1 and pr3 but not pr2"
    assert PullRequests.search_count([('parent_id', '=', pr1_b_id.id)])
    assert PullRequests.search_count([('parent_id', '=', pr3_b_id.id)])

    assert repo.get_pr(pr1_b_id.number).comments == [
        seen(env, repo.get_pr(pr1_b_id.number), users),
        (users['user'], "This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.\n\nMore info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port\n"),
        (users['user'], "@{user} @{reviewer} the target branch 'b' has been disabled, you may want to close this PR.\n\nAs this was not its limit, it will automatically be forward ported to the next active branch.".format_map(users)),
    ]
    assert repo2.get_pr(pr2_b_id.number).comments == [
        seen(env, repo2.get_pr(pr2_b_id.number), users),
        (users['user'], """\
@{user} @{reviewer} this PR targets b and is the last of the forward-port chain.

To merge the full chain, use
> @hansen r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""".format_map(users)),
        (users['user'], "@{user} @{reviewer} the target branch 'b' has been disabled, you may want to close this PR.".format_map(users)),
    ]

def test_disable_multitudes(env, config, make_repo, users, setreviewers):
    """Ensure that deactivation ports can jump over other deactivated branches.
    """
    # region setup
    repo = make_repo("bob")
    project = env['runbot_merge.project'].create({
        "name": "bob",
        "github_token": config['github']['token'],
        "github_prefix": "hansen",
        'github_name': config['github']['name'],
        'github_email': "foo@example.org",
        "fp_github_token": config['github']['token'],
        "fp_github_name": "herbert",
        "branch_ids": [
            (0, 0, {'name': 'a', 'sequence': 90}),
            (0, 0, {'name': 'b', 'sequence': 80}),
            (0, 0, {'name': 'c', 'sequence': 70}),
            (0, 0, {'name': 'd', 'sequence': 60}),
        ],
        "repo_ids": [(0, 0, {
            'name': repo.name,
            'required_statuses': 'default',
            'fp_remote_target': repo.name,
        })],
    })
    setreviewers(project.repo_ids)
    env['runbot_merge.events_sources'].create({'repository': repo.name})

    with repo:
        [a, b, c, d] = repo.make_commits(
            None,
            Commit("a", tree={"branch": "a"}),
            Commit("b", tree={"branch": "b"}),
            Commit("c", tree={"branch": "c"}),
            Commit("d", tree={"branch": "d"}),
        )
        repo.make_ref("heads/a", a)
        repo.make_ref("heads/b", b)
        repo.make_ref("heads/c", c)
        repo.make_ref("heads/d", d)
    # endregion

    with repo:
        [a] = repo.make_commits("a", Commit("X", tree={"x": "1"}), ref="heads/x")
        pra = repo.make_pr(target="a", head="x")
        pra.post_comment("hansen r+", config['role_reviewer']['token'])
        repo.post_status(a, "success")
    env.run_crons()

    with repo:
        repo.post_status('staging.a', 'success')
    env.run_crons()

    pra_id = to_pr(env, pra)
    assert pra_id.state == 'merged'

    prb_id = env['runbot_merge.pull_requests'].search([('target.name', '=', 'b')])
    assert prb_id.parent_id == pra_id

    project.write({
        'branch_ids': [
            (1, b.id, {'active': False})
            for b in env['runbot_merge.branch'].search([('name', 'in', ['b', 'c'])])
        ]
    })
    env.run_crons()

    # should not have ported prb to the disabled branch c
    assert not env['runbot_merge.pull_requests'].search([('target.name', '=', 'c')])

    # should have ported prb to the active branch d
    prd_id = env['runbot_merge.pull_requests'].search([('target.name', '=', 'd')])
    assert prd_id
    assert prd_id.parent_id == prb_id

    prb = repo.get_pr(prb_id.number)
    assert prb.comments == [
        seen(env, prb, users),
        (users['user'], 'This PR targets b and is part of the forward-port chain. Further PRs will be created up to d.\n\nMore info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port\n'),
        (users['user'], """\
@{user} @{reviewer} the target branch 'b' has been disabled, you may want to close this PR.

As this was not its limit, it will automatically be forward ported to the next active branch.\
""".format_map(users)),
    ]
    prd = repo.get_pr(prd_id.number)
    assert prd.comments == [
        seen(env, prd, users),
        (users['user'], """\
@{user} @{reviewer} this PR targets d and is the last of the forward-port chain.

To merge the full chain, use
> @hansen r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""".format_map(users))
    ]

FMT = '%Y-%m-%d %H:%M:%S'
FAKE_PREV_WEEK = (datetime.now() + timedelta(days=1)).strftime(FMT)
def test_reminder_detached(env, config, make_repo, users):
    """On detached forward ports, both sides of the detachment should be notified.
    """
    # region setup
    prod, _ = make_basic(env, config, make_repo, statuses='default')
    with prod:
        prod.make_commits('a', Commit('c', tree={'x': '0'}), ref="heads/abranch")
        pr_a = prod.make_pr(target='a', head='abranch')
        prod.post_status('abranch', 'success')
        pr_a.post_comment('hansen r+ fw=skipci', config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    pr_a_id = to_pr(env, pr_a)
    pr_b_id = env['runbot_merge.pull_requests'].search([
        ('target.name', '=', 'b'),
        ('parent_id', '=', pr_a_id.id),
    ])
    assert pr_b_id
    with prod:
        prod.post_status(pr_b_id.head, 'success')
    env.run_crons()
    pr_c_id = env['runbot_merge.pull_requests'].search([
        ('target.name', '=', 'c'),
        ('parent_id', '=', pr_b_id.id),
    ])
    assert pr_c_id
    # endregion

    pr_b = prod.get_pr(pr_b_id.number)
    pr_c = prod.get_pr(pr_c_id.number)

    # region sanity check
    (pr_a_id | pr_b_id | pr_c_id).reminder_next = datetime.now() - timedelta(days=1)
    env.run_crons('runbot_merge.reminder')

    assert pr_b.comments == [
        seen(env, pr_b, users),
        (users['user'], """\
This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""")], "the intermediate PR should not be reminded"

    assert pr_c.comments == [
        seen(env, pr_c, users),
        (users['user'], """\
@%s @%s this PR targets c and is the last of the forward-port chain containing:
* %s

To merge the full chain, use
> @hansen r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""" % (
            users['user'], users['reviewer'],
            pr_b_id.display_name,
    )),
        (users['user'], "@%s @%s this forward port of %s is awaiting action (not merged or closed)." % (
            users['user'],
            users['reviewer'],
            pr_a_id.display_name,
        ))
    ], "the final PR should be reminded"
    # endregion

    # region check detached
    pr_c_id.write({'parent_id': False, 'detach_reason': 'because'})
    (pr_a_id | pr_b_id | pr_c_id).reminder_next = datetime.now() - timedelta(days=1)
    env.run_crons('runbot_merge.reminder')

    assert pr_b.comments[2:] == [
        (users['user'], "@%s @%s child PR %s has become a normal PR because because. This PR (and any of its parents) will need to be merged independently as approvals won't cross." % (
            users['user'],
            users['reviewer'],
            pr_c_id.display_name,
        )),
        (users['user'], "@%s @%s this forward port of %s is awaiting action (not merged or closed)." % (
            users['user'],
            users['reviewer'],
            pr_a_id.display_name,
        ))
    ], "the detached-from intermediate PR should now be reminded"
    assert pr_c.comments[3:] == [
        (users['user'], "@%s @%s this forward port of %s is awaiting action (not merged or closed)." % (
            users['user'],
            users['reviewer'],
            pr_a_id.display_name,
        ))
    ], "the final forward port should be reminded as before"
    # endregion

def test_duplicate_manual_port(env, config, make_repo, users):
    """Most forward port checks are performed before creating the fw batch, but
    the FW batch processor should still avoid creating duplicate forward ports.
    """
    # region setup
    prod, _ = make_basic(env, config, make_repo, statuses='default')
    with prod:
        prod.make_commits('a', Commit('c', tree={'x': '0'}), ref="heads/abranch")
        pr_a = prod.make_pr(target='a', head='abranch')
        prod.post_status('abranch', 'success')
        pr_a.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    PullRequests = env['runbot_merge.pull_requests']
    pr_a_id = to_pr(env, pr_a)
    for branch in 'bc':
        child_id = PullRequests.search([
            ('target.name', '=', branch),
            ('source_id', '=', pr_a_id.id),
        ])
        with prod:
            prod.post_status(child_id.head, 'success')
        env.run_crons()
    _, pr_b_id, _ = PullRequests.search([], order='number')

    env['forwardport.batches'].create({
        'batch_id': pr_b_id.batch_id.id,
        'source': 'complete',
        'pr_id': pr_b_id.id,
    })

    env['forwardport.batches'].create({
        'batch_id': pr_b_id.batch_id.id,
        'source': 'fp',
    })
    env.run_crons()

@pytest.mark.parametrize('triggered', range(4))
@pytest.mark.parametrize('kind', ['merge', 'fp'])
def test_resume_manual_detached(env, config, make_repo, users, triggered, kind):
    """In case of detached interrupted forward ports, check where and how they
    can be manually resumed from.

    Turns out it always works, not sure why on the mergebot there seemingly
    have been cases where creating a batch from the source batch with source=fp
    sometimes didn't work...
    """
    # region setup
    prod, _ = make_basic(env, config, make_repo, statuses='default')
    with prod:
        prod.make_commits('a', Commit('c', tree={'x': '0'}), ref="heads/abranch")
        pr1 = prod.make_pr(target='a', head='abranch')
        prod.post_status('abranch', 'success')
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])
    with prod:
        prod.make_commits('a', Commit('cc', tree={'y': '0'}), ref="heads/bbranch")
        pr2 = prod.make_pr(target='a', head='bbranch')
        prod.post_status('bbranch', 'success')
        pr2.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    pr1_a_id, pr2_a_id, pr1_b_id, pr2_b_id = prs = env['runbot_merge.pull_requests'].search([], order='number')
    assert pr1_b_id.source_id == pr1_a_id
    assert pr2_b_id.source_id == pr2_a_id
    pr1_b_id.write({
        'parent_id': False,
        'detach_reason': 'whatever',
    })

    proj = env['runbot_merge.project'].search([])
    proj.write({
        'branch_ids': [
            (1, pr1_b_id.target.id, {'active': False}),
        ]
    })
    fw_batches = env['forwardport.batches'].search([])
    assert len(fw_batches) == 2
    fw_batches.unlink()

    env.run_crons()
    assert env['runbot_merge.pull_requests'].search_count([]) == 4
    # endregion

    # At this point we have two merged source PRs to A and two forward ports
    # to the now disabled B, one detached and the other not.
    #
    # The port was interrupted because reasons even though the branch being
    # disabled should have forced ports to C (it's happened live for a few
    # reasons we have added guards for, still we want to handle recovery if a
    # new issue sneaks in).
    #
    # We want to see if creating fw batches by hand puts us back into gear.
    pr = prs[triggered]
    env['forwardport.batches'].create({'source': kind, 'batch_id': pr.id})
    env.run_crons()

    new_prs = env['runbot_merge.pull_requests'].search([], order='number')[len(prs):]
    assert len(new_prs) == 1, f"{pr.display_name} ({kind}) => no port (expected one)"
    assert new_prs.source_id == (pr.source_id or pr)

    env['forwardport.batches'].create([
        {'source': 'merge', 'batch_id': pr.id},
        {'source': 'fp', 'batch_id': pr.id},
    ])
    env.run_crons()
    assert not env['runbot_merge.pull_requests'].search([], order='number')[len(prs)+1:]


@pytest.mark.expect_log_errors(reason="push conflict triggers a logged exception")
def test_table_on_fp_interruption(env, config, make_repo, users, page, port):
    """If the forwardport fails for some reason in this case simulated by having
    an existing branch with the same name, which can and has happened), the
    genealogy table should still display rows up to the limit.
    """
    prod, other = make_basic(env, config, make_repo, statuses='default')

    with prod:
        prod.make_commits('a', Commit('c', tree={'x': '0'}), ref="heads/abranch")
        pr = prod.make_pr(target='a', head='abranch')
        prod.post_status('abranch', 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    pr_id = to_pr(env, pr)
    with other:
        # fp pattern is `{target.name}-{source.target.name}-{batch.id}-fw`
        other.make_commits(
            None,
            Commit('com-blocked', tree={'f': '1'}),
            ref="heads/b-abranch-%d-fw" % pr_id.batch_id.id,
        )

    doc = pr_page(page, pr)
    [tbody] = doc.cssselect(".table-bordered tbody")
    assert len(tbody) == 3, "branches should be a, b, c"

    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    assert env['runbot_merge.pull_requests'].search_count([]) == 1,\
        "should not have been able to create the forward port"
    assert env['forwardport.batches'].search_count([]) == 1,\
        "fw batch should still be enqueued"

    doc = pr_page(page, pr)
    [tbody] = doc.cssselect(".table-bordered tbody")
    assert len(tbody) == 3, "branches should still be a, b, c"

@pytest.mark.parametrize('delete_branch', [True, False])
def test_merge_after_followup_closed(env, config, make_repo, users, delete_branch):
    """ If a forward port's followup is closed then the forward port is
    merged, the mergebot should not try to re-create the followup.
    Even if the forward port is detached.
    """
    prod, dev = make_basic(env, config, make_repo, statuses='default')
    with prod:
        prod.make_commits('a', Commit('c', tree={'x': '0'}), ref="heads/abranch")
        pr = prod.make_pr(target='a', head='abranch')
        prod.post_status('abranch', 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success')
    env.run_crons()

    pra_id, prb_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert prb_id.parent_id == pra_id
    with prod:
        prod.post_status(prb_id.head, 'success')
    env.run_crons()

    _pra_id, _prb_id, prc_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert prc_id.parent_id == prb_id
    prb_id.write({
        'parent_id': False,
        'detach_reason': 'because',
    })

    prc = prod.get_pr(prc_id.number)
    with prod:
        prc.close()
    if delete_branch:
        with dev:
            dev.delete_ref(prc.ref)
    env.run_crons()
    assert prc_id.state == 'closed'

    # add a new commit to `c` to highlight the re-porting
    with prod:
        prod.make_commits('c', Commit('new commit', tree={'h': 'b'}), ref='heads/c')

    with prod:
        prod.get_pr(prb_id.number).post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.b', 'success')
    env.run_crons()

    assert pra_id.state == 'merged'
    assert prb_id.state == 'merged'

    env.run_crons()

    assert env['forwardport.batches'].search_count([]) == 0
    _pra_id, _prb_id, _prc_id = env['runbot_merge.pull_requests'].search([], order='number')


def test_mergiraf_merge_crash(env, config, make_repo, users, tmp_path, server):
    """If mergiraf crashes we should just merge normally (without mergiraf)
    """
    repo, _ = make_basic(env, config, make_repo, statuses='default')
    project = env['runbot_merge.project'].search([])
    mergiraf_path = tmp_path.joinpath("bin", "mergiraf")
    mergiraf_path.write_text("""\
#!/bin/sh

kill -6 $$
""")
    mergiraf_path.chmod(0o500)
    project.use_mergiraf = True
    assert not project.warn_mergiraf
    with repo:
        m1, _ = repo.make_commits(
            None,
            Commit('initial', tree={'f.py': '''\
def f():
    a = 0
    return a
'''}),
            Commit('second', tree={'f.py': '''\
def f():
    a = 1
    return a
'''}),
            ref='heads/a',
        )

        [c2] = repo.make_commits(
            m1,
            Commit('other second', tree={'f.py': '''\
def f():
    b = 0
    return b
'''}),
        )
        pr = repo.make_pr(title='title', body='body', target='a', head=c2)
        repo.post_status(pr.head, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    # staging failure where this PR is the first candidate => pr is in error
    assert to_pr(env, pr).state == 'error'
    assert b'mergiraf likely crashed' in server[1]


def test_mergiraf_fw_crash(env, config, make_repo, users, tmp_path, server):
    """If mergiraf crashes we should just merge normally (without mergiraf)
    """
    repo, _ = make_basic(env, config, make_repo, statuses='default')
    project = env['runbot_merge.project'].search([])
    project.use_mergiraf = True
    assert not project.warn_mergiraf
    with repo:
        m1, _ = repo.make_commits(
            None,
            Commit('initial', tree={'f.py': '''\
def f():
    a = 0
    return a
'''}),
            Commit('second', tree={'f.py': '''\
def f():
    a = 1
    return a
'''}),
            ref='heads/b',
        )
        repo.update_ref('heads/a', m1, force=True)

        [c2] = repo.make_commits(
            m1,
            Commit('other second', tree={'f.py': '''\
def f():
    b = 0
    return b
'''}),
        )
        pr = repo.make_pr(title='title', body='body', target='a', head=c2)
        repo.post_status(pr.head, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    # now add a crashy mergiraf for forward-porting
    mergiraf_path = tmp_path.joinpath("bin", "mergiraf")
    mergiraf_path.write_text("""\
#!/bin/sh

kill -6 $$
""")
    mergiraf_path.chmod(0o500)
    with repo:
        repo.post_status('staging.a', 'success')
    env.run_crons()
    assert b'mergiraf likely crashed' in server[1]

    pr_id = to_pr(env, pr)
    assert pr_id.state == 'merged'
    _, fw_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert fw_id.source_id == pr_id

    assert repo.read_tree(repo.commit(fw_id.head)) == {
        "f.py": matches("""\
def f():
<<<\x3c<<< $$
    a = 1
    return a
||||||| $$
    a = 0
    return a
=======
    b = 0
    return b
>>>\x3e>>> $$
""")
    }


def test_approve_fw_no_email(env, make_repo, users, config, partners):
    """Even if there are multiple outstanding forward ports getting approved,
    each error should be posted only once, no need to tell the user that we
    don't know their email for every forward port we attempted to approve.
    """
    repo, _ = make_basic(env, config, make_repo, statuses='default')
    with repo:
        [c] = repo.make_commits('a', Commit('first', tree={'x': '42'}))
        pr_a = repo.make_pr(target='a', head=c)
        repo.post_status(pr_a.head, 'success')
        pr_a.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with repo:
        repo.post_status('staging.a', 'success')
    env.run_crons()
    pr_a_id, pr_b_id = env['runbot_merge.pull_requests'].search([], order='number')
    with repo:
        repo.post_status(pr_b_id.head, 'success')
    env.run_crons()
    _, _, pr_c_id = env['runbot_merge.pull_requests'].search([], order='number')

    user_partner = env['res.partner'].search([('github_login', '=', users['user'])])
    assert user_partner.email is False
    pr_c = repo.get_pr(pr_c_id.number)
    with repo:
        pr_c.post_comment('hansen r+', config['role_user']['token'])
    env.run_crons()

    assert pr_c.comments == [
        seen(env, pr_c, users),
        (users['user'], f"""\
@{users['user']} @{users['reviewer']} this PR targets c and is the last of the forward-port chain containing:
* {pr_b_id.display_name}

To merge the full chain, use
> @hansen r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
"""),
        (users['user'], 'hansen r+'),
        (users['user'], f"@{users['user']} I must know your email before you can review PRs. Please contact an administrator."),
    ]
    user_partner.fetch_github_email()
    assert user_partner.email
    with repo:
        pr_c.post_comment('hansen r+', config['role_user']['token'])
    env.run_crons()
    assert pr_c_id.state == 'approved'

def test_close_blocker(
    env, make_repo, users, config, partners,
):
    repo1, _ = make_basic(env, config, make_repo, statuses='default')
    repo2, fw_repo = make_basic(env, config, make_repo, statuses='default')
    with repo1:
        repo1.make_commits('a', Commit('first', tree={'x': '42'}), ref="heads/abranch")
        pr_a1 = repo1.make_pr(target='a', head='abranch')
        repo1.post_status(pr_a1.head, 'success')
        pr_a1.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with repo1, repo2:
        repo1.post_status('staging.a', 'success')
        repo2.post_status('staging.a', 'success')
    env.run_crons()
    pr_a1_id, pr_b1_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert fw_repo.owner == pr_b1_id.label.split(':')[0]
    with repo2, fw_repo:
        fw_repo.make_commits(
            repo2.commit('b').id,
            Commit('second', tree={'x': '42'}),
            ref=f"heads/{pr_b1_id.refname}",
        )
        pr_b2 = repo2.make_pr(title="a pr", target='b', head=pr_b1_id.label)
    assert len(pr_b1_id.batch_id.prs) == 2
    with repo1:
        repo1.post_status(pr_b1_id.head, 'success')
    env.run_crons()

    pr_b2_id = to_pr(env, pr_b2)
    assert pr_b2.number == pr_b2_id.number
    with repo2:
        pr_b2.close()
    env.run_crons()

    assert pr_b2_id.closed
    assert pr_b2_id.state == 'closed'
    assert env['runbot_merge.pull_requests'].search_count([]) == 4,\
        "should have triggered fw when we closed the PR"

    with repo1:
        repo1.get_pr(pr_b1_id.number).post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with repo1, repo2:
        repo1.post_status('staging.b', 'success')
        repo2.post_status('staging.b', 'success')
    env.run_crons()

    assert pr_b1_id.state == 'merged'
    assert env['runbot_merge.pull_requests'].search_count([]) == 4