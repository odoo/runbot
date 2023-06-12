# -*- coding: utf-8 -*-
from datetime import datetime

import pytest

from utils import seen, Commit, to_pr


def make_basic(env, config, make_repo, *, fp_token, fp_remote):
    """ Creates a basic repo with 3 forking branches

    0 -- 1 -- 2 -- 3 -- 4  : a
              |
              `-- 11 -- 22 : b
                  |
                  `-- 111  : c
    each branch just adds and modifies a file (resp. f, g and h) through the
    contents sequence a b c d e
    """
    Projects = env['runbot_merge.project']
    project = Projects.search([('name', '=', 'myproject')])
    if not project:
        project = Projects.create({
            'name': 'myproject',
            'github_token': config['github']['token'],
            'github_prefix': 'hansen',
            'fp_github_token': fp_token and config['github']['token'],
            'fp_github_name': 'herbert',
            'fp_github_email': 'hb@example.com',
            'branch_ids': [
                (0, 0, {'name': 'a', 'sequence': 2}),
                (0, 0, {'name': 'b', 'sequence': 1}),
                (0, 0, {'name': 'c', 'sequence': 0}),
            ],
        })

    prod = make_repo('proj')
    with prod:
        a_0, a_1, a_2, a_3, a_4, = prod.make_commits(
            None,
            Commit("0", tree={'f': 'a'}),
            Commit("1", tree={'f': 'b'}),
            Commit("2", tree={'f': 'c'}),
            Commit("3", tree={'f': 'd'}),
            Commit("4", tree={'f': 'e'}),
            ref='heads/a',
        )
        b_1, b_2 = prod.make_commits(
            a_2,
            Commit('11', tree={'g': 'a'}),
            Commit('22', tree={'g': 'b'}),
            ref='heads/b',
        )
        prod.make_commits(
            b_1,
            Commit('111', tree={'h': 'a'}),
            ref='heads/c',
        )
    other = prod.fork()
    repo = env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': prod.name,
        'required_statuses': 'legal/cla,ci/runbot',
        'fp_remote_target': fp_remote and other.name,
    })
    env['res.partner'].search([
        ('github_login', '=', config['role_reviewer']['user'])
    ]).write({
        'review_rights': [(0, 0, {'repository_id': repo.id, 'review': True})]
    })
    env['res.partner'].search([
        ('github_login', '=', config['role_self_reviewer']['user'])
    ]).write({
        'review_rights': [(0, 0, {'repository_id': repo.id, 'self_review': True})]
    })

    return project, prod, other

def test_no_token(env, config, make_repo):
    """ if there's no token on the repo, nothing should break though should
    log
    """
    # create project configured with remotes on the repo but no token
    proj, prod, _ = make_basic(env, config, make_repo, fp_token=False, fp_remote=True)

    with prod:
        prod.make_commits(
            'a', Commit('c0', tree={'a': '0'}), ref='heads/abranch'
        )
        pr = prod.make_pr(target='a', head='abranch')
        prod.post_status(pr.head, 'success', 'legal/cla')
        prod.post_status(pr.head, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

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
    proj, prod, _ = make_basic(env, config, make_repo, fp_token=True, fp_remote=True)
    proj.fp_github_token = False

    with prod:
        prod.make_commits(
            'a', Commit('c0', tree={'a': '0'}), ref='heads/abranch'
        )
        pr = prod.make_pr(target='a', head='abranch')
        prod.post_status(pr.head, 'success', 'legal/cla')
        prod.post_status(pr.head, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

    env.run_crons()
    assert len(env['runbot_merge.pull_requests'].search([], order='number')) == 1,\
        "should not have created forward port"

def test_no_target(env, config, make_repo):
    proj, prod, _ = make_basic(env, config, make_repo, fp_token=True, fp_remote=False)

    with prod:
        prod.make_commits(
            'a', Commit('c0', tree={'a': '0'}), ref='heads/abranch'
        )
        pr = prod.make_pr(target='a', head='abranch')
        prod.post_status(pr.head, 'success', 'legal/cla')
        prod.post_status(pr.head, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

    env.run_crons()
    assert len(env['runbot_merge.pull_requests'].search([], order='number')) == 1,\
        "should not have created forward port"

def test_failed_staging(env, config, make_repo):
    proj, prod, _ = make_basic(env, config, make_repo, fp_token=True, fp_remote=True)

    reviewer = config['role_reviewer']['token']
    with prod:
        prod.make_commits('a', Commit('c', tree={'a': '0'}), ref='heads/abranch')
        pr1 = prod.make_pr(target='a', head='abranch')
        prod.post_status(pr1.head, 'success', 'legal/cla')
        prod.post_status(pr1.head, 'success', 'ci/runbot')
        pr1.post_comment('hansen r+', reviewer)
    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()

    pr1_id, pr2_id = env['runbot_merge.pull_requests'].search([], order='number')
    assert pr2_id.parent_id == pr2_id.source_id == pr1_id
    with prod:
        prod.post_status(pr2_id.head, 'success', 'legal/cla')
        prod.post_status(pr2_id.head, 'success', 'ci/runbot')
    env.run_crons()

    pr1_id, pr2_id, pr3_id = env['runbot_merge.pull_requests'].search([], order='number')
    pr3 = prod.get_pr(pr3_id.number)
    with prod:
        prod.post_status(pr3_id.head, 'success', 'legal/cla')
        prod.post_status(pr3_id.head, 'success', 'ci/runbot')
        pr3.post_comment('%s r+' % proj.fp_github_name, reviewer)
    env.run_crons()

    prod.commit('staging.c')

    with prod:
        prod.post_status('staging.b', 'success', 'legal/cla')
        prod.post_status('staging.b', 'success', 'ci/runbot')
        prod.post_status('staging.c', 'failure', 'ci/runbot')
    env.run_crons()

    pr3_head = env['runbot_merge.commit'].search([
        ('sha', '=', pr3_id.head),
    ])
    assert len(pr3_head) == 1

    assert not pr3_id.batch_id, "check that the PR indeed has no batch anymore"
    assert not pr3_id.batch_ids.filtered(lambda b: b.active)

    assert len(env['runbot_merge.batch'].search([
        ('prs', 'in', pr3_id.id),
        '|', ('active', '=', True),
             ('active', '=', False),
    ])) == 2, "check that there do exist batches"

    # send a new status to the PR, as if somebody had rebuilt it or something
    with prod:
        pr3.post_comment('hansen retry', reviewer)
        prod.post_status(pr3_id.head, 'success', 'foo/bar')
        prod.post_status(pr3_id.head, 'success', 'legal/cla')
    assert pr3_head.to_check, "check that the commit was updated as to process"
    env.run_crons()
    assert not pr3_head.to_check, "check that the commit was processed"

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
            'fp_github_token': config['github']['token'],
            'fp_github_name': 'herbert',
            'fp_github_email': 'hb@example.com',
            'branch_ids': [
                (0, 0, {'name': 'a', 'sequence': 2}),
                (0, 0, {'name': 'b', 'sequence': 1}),
                (0, 0, {'name': 'c', 'sequence': 0}),
            ]
        })
        repo_a = env['runbot_merge.repository'].create({
            'project_id': project.id,
            'name': a.name,
            'required_statuses': 'ci/runbot',
            'fp_remote_target': a_dev.name,
        })
        repo_b = env['runbot_merge.repository'].create({
            'project_id': project.id,
            'name': b.name,
            'required_statuses': 'ci/runbot',
            'fp_remote_target': b_dev.name,
            'branch_filter': '[("name", "in", ["a", "c"])]',
        })
        setreviewers(repo_a, repo_b)
        return project, a, a_dev, b, b_dev

    def test_single_first(self, env, repos, config):
        """ A merge in A.a should be forward-ported to A.b and A.c
        """
        project, a, a_dev, b, _ = repos
        with a, a_dev:
            [c] = a_dev.make_commits('a', Commit('pr', tree={'pr': '1'}), ref='heads/change')
            pr = a.make_pr(target='a', title="a pr", head=a_dev.owner + ':change')
            a.post_status(c, 'success', 'ci/runbot')
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
        p = env['runbot_merge.pull_requests'].search([('repository.name', '=', a.name), ('number', '=', pr.number)])
        env.run_crons()
        assert p.staging_id
        with a, b:
            for repo in a, b:
                repo.post_status('staging.a', 'success', 'ci/runbot')
        env.run_crons()

        a_head = a.commit('a')
        assert a_head.message.startswith('pr\n\n')
        assert a.read_tree(a_head) == {'a': '2', 'pr': '1'}

        pr0, pr1 = env['runbot_merge.pull_requests'].search([], order='number')
        with a:
            a.post_status(pr1.head, 'success', 'ci/runbot')
        env.run_crons()

        pr0, pr1, pr2 = env['runbot_merge.pull_requests'].search([], order='number')
        with a:
            a.post_status(pr2.head, 'success', 'ci/runbot')
            a.get_pr(pr2.number).post_comment(
                '%s r+' % project.fp_github_name,
                config['role_reviewer']['token'])
        env.run_crons()
        assert pr1.staging_id
        assert pr2.staging_id
        with a, b:
            a.post_status('staging.b', 'success', 'ci/runbot')
            a.post_status('staging.c', 'success', 'ci/runbot')
            b.post_status('staging.c', 'success', 'ci/runbot')
        env.run_crons()

        assert pr0.state == 'merged'
        assert pr1.state == 'merged'
        assert pr2.state == 'merged'
        assert a.read_tree(a.commit('b')) == {'a': '1', 'b': '22',             'pr': '1'}
        assert a.read_tree(a.commit('c')) == {'a': '1', 'b': '11', 'c': '111', 'pr': '1'}

    def test_single_second(self, env, repos, config):
        """ A merge in B.a should "skip ahead" to B.c
        """
        project, a, _, b, b_dev = repos
        with b, b_dev:
            [c] = b_dev.make_commits('a', Commit('pr', tree={'pr': '1'}), ref='heads/change')
            pr = b.make_pr(target='a', title="a pr", head=b_dev.owner + ':change')
            b.post_status(c, 'success', 'ci/runbot')
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with a, b:
            a.post_status('staging.a', 'success', 'ci/runbot')
            b.post_status('staging.a', 'success', 'ci/runbot')
        env.run_crons()

        assert b.read_tree(b.commit('a')) == {'a': 'z', 'pr': '1'}

        pr0, pr1 = env['runbot_merge.pull_requests'].search([], order='number')
        with b:
            b.post_status(pr1.head, 'success', 'ci/runbot')
            b.get_pr(pr1.number).post_comment(
                '%s r+' % project.fp_github_name,
                config['role_reviewer']['token'])
        env.run_crons()
        with a, b:
            a.post_status('staging.c', 'success', 'ci/runbot')
            b.post_status('staging.c', 'success', 'ci/runbot')
        env.run_crons()

        assert pr0.state == 'merged'
        assert pr1.state == 'merged'
        assert b.read_tree(b.commit('c')) == {'a': 'y', 'c': 'x', 'pr': '1'}

    def test_both_first(self, env, repos, config, users):
        """ A merge in A.a, B.a should... not be forward-ported at all?
        """
        project, a, a_dev, b, b_dev = repos
        with a, a_dev:
            [c_a] = a_dev.make_commits('a', Commit('pr a', tree={'pr': 'a'}), ref='heads/change')
            pr_a = a.make_pr(target='a', title='a pr', head=a_dev.owner + ':change')
            a.post_status(c_a, 'success', 'ci/runbot')
            pr_a.post_comment('hansen r+', config['role_reviewer']['token'])
        with b, b_dev:
            [c_b] = b_dev.make_commits('a', Commit('pr b', tree={'pr': 'b'}), ref='heads/change')
            pr_b = b.make_pr(target='a', title='b pr', head=b_dev.owner + ':change')
            b.post_status(c_b, 'success', 'ci/runbot')
            pr_b.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with a, b:
            for repo in a, b:
                repo.post_status('staging.a', 'success', 'ci/runbot')
        env.run_crons()

        pr_a_id = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', a.name),
            ('number', '=', pr_a.number),
        ])
        pr_b_id = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', b.name),
            ('number', '=', pr_b.number)
        ])
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
    def validate(repo, commit):
        repo.post_status(commit, 'success', 'ci/runbot')
        repo.post_status(commit, 'success', 'legal/cla')
    project, prod, _ = make_basic(env, config, make_repo, fp_token=True, fp_remote=True)
    _, prod2, _ = make_basic(env, config, make_repo, fp_token=True, fp_remote=True)
    assert len(project.repo_ids) == 2

    original_c_tree = prod.read_tree(prod.commit('c'))
    prs = []
    with prod, prod2:
        for i in ['0', '1']:
            prod.make_commits('a', Commit(i, tree={i:i}), ref='heads/branch%s' % i)
            pr = prod.make_pr(target='a', head='branch%s' % i)
            prs.append(pr)
            validate(prod, pr.head)
            pr.post_comment('hansen r+', config['role_reviewer']['token'])

        # also add a PR targeting b forward-ported to c, in order to check
        # for an insertion right after the source, as well as have linked PRs in
        # two different repos
        prod.make_commits('b', Commit('x', tree={'x': 'x'}), ref='heads/branchx')
        prod2.make_commits('b', Commit('x2', tree={'x': 'x2'}), ref='heads/branchx')
        prx = prod.make_pr(target='b', head='branchx')
        prx2 = prod2.make_pr(target='b', head='branchx')
        validate(prod, prx.head)
        validate(prod2, prx2.head)
        prx.post_comment('hansen r+', config['role_reviewer']['token'])
        prx2.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod, prod2:
        for r in [prod, prod2]:
            validate(r, 'staging.a')
            validate(r, 'staging.b')
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
        validate(prod, pr0_fp_id.head)
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
            validate(get_repo(fp), fp.head)
    env.run_crons()
    # now fps should be the last PR of each sequence, and thus r+-able (via
    # fwbot so preceding PR is also r+'d)
    with prod, prod2:
        for pr in fps.filtered(lambda p: p.target.name == 'c'):
            get_repo(pr).get_pr(pr.number).post_comment(
                '%s r+' % project.fp_github_name,
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
            validate(prod, f'staging.{target}')
            validate(prod2, f'staging.{target}')
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
    project, prod, xxx = make_basic(env, config, make_repo, fp_token=True, fp_remote=True)
    other_user = config['role_other']
    other_token = other_user['token']
    other = prod.fork(token=other_token)

    with prod, other:
        [c] = other.make_commits('a', Commit('c', tree={'0': '0'}), ref='heads/change')
        pr = prod.make_pr(
            target='a', title='my change',
            head=other_user['user'] + ':change',
            token=other_token
        )
        # should be able to close and open own PR
        pr.close(other_token)
        pr.open(other_token)
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('%s close' % project.fp_github_name, other_token)
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    assert pr.state == 'open'

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
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
        pr1.post_comment('%s close' % project.fp_github_name, other_token)
    env.run_crons()
    assert pr1.state == 'closed'
    assert pr1_id.state == 'closed'

def test_skip_ci_all(env, config, make_repo):
    project, prod, _ = make_basic(env, config, make_repo, fp_token=True, fp_remote=True)

    with prod:
        prod.make_commits('a', Commit('x', tree={'x': '0'}), ref='heads/change')
        pr = prod.make_pr(target='a', head='change')
        prod.post_status(pr.head, 'success', 'legal/cla')
        prod.post_status(pr.head, 'success', 'ci/runbot')
        pr.post_comment('%s skipci' % project.fp_github_name, config['role_reviewer']['token'])
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    assert env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', prod.name),
        ('number', '=', pr.number)
    ]).fw_policy == 'skipci'

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
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
    project, prod, _ = make_basic(env, config, make_repo, fp_token=True, fp_remote=True)

    with prod:
        prod.make_commits('a', Commit('x', tree={'x': '0'}), ref='heads/change')
        pr = prod.make_pr(target='a', head='change')
        prod.post_status(pr.head, 'success', 'legal/cla')
        prod.post_status(pr.head, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()

    pr0_id, pr1_id = env['runbot_merge.pull_requests'].search([], order='number')
    with prod:
        prod.get_pr(pr1_id.number).post_comment(
            '%s skipci' % project.fp_github_name,
            config['role_user']['token']
        )
    assert pr0_id.fw_policy == 'skipci'
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
    project, prod, _ = make_basic(env, config, make_repo, fp_token=True, fp_remote=True)
    with prod:
        [c] = prod.make_commits('b', Commit('thing', tree={'x': '1'}), ref='heads/mypr')
        pr = prod.make_pr(target='b', head='mypr')
        prod.post_status(c, 'success', 'ci/runbot')
        prod.post_status(c, 'success', 'legal/cla')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    original_pr_id = to_pr(env, pr)
    assert original_pr_id.state == 'ready'
    assert original_pr_id.staging_id

    with prod:
        prod.post_status('staging.b', 'success', 'ci/runbot')
        prod.post_status('staging.b', 'success', 'legal/cla')
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

    env.run_crons('forwardport.port_forward')
    assert not job.exists(), "job should have succeeded and apoptosed"

    # since the PR was "already forward-ported" to the new branch it should not
    # be touched
    assert env['runbot_merge.pull_requests'].search([('state', 'not in', ('merged', 'closed'))]) == port_id

    # merge the retargered PR
    with prod:
        prod.post_status(port_pr.head, 'success', 'ci/runbot')
        prod.post_status(port_pr.head, 'success', 'legal/cla')
        port_pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.bprime', 'success', 'ci/runbot')
        prod.post_status('staging.bprime', 'success', 'legal/cla')
    env.run_crons()

    new_pr_id = env['runbot_merge.pull_requests'].search([('state', 'not in', ('merged', 'closed'))])
    assert len(new_pr_id) == 1
    assert new_pr_id.parent_id == port_id
    assert new_pr_id.target == branch_c

def test_approve_draft(env, config, make_repo, users):
    _, prod, _ = make_basic(env, config, make_repo, fp_token=True, fp_remote=True)

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
        (users['user'], f"I'm sorry, @{users['reviewer']}: draft PRs can not be approved."),
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
    """
    project, prod, _ = make_basic(env, config, make_repo, fp_token=True, fp_remote=True)
    # branches here are "a" (older), "b", and "c" (master)
    with prod:
        [root, _] = prod.make_commits(
            None,
            Commit('base', tree={'version': '', 'f': '0'}),
            Commit('release 1.0', tree={'version': '1.0'}),
            ref='heads/b'
        )
        prod.make_commits(root, Commit('other', tree={'f': '1'}), ref='heads/c')
    with prod:
        prod.make_commits(
            'c',
            Commit('Release 1.1', tree={'version': '1.1'}),
            ref='heads/release-1.1'
        )
        release = prod.make_pr(target='c', head='release-1.1')
    env.run_crons()

    w = project.action_prepare_freeze()
    assert w['res_model'] == 'runbot_merge.project.freeze'
    w_id = env[w['res_model']].browse([w['res_id']])
    assert w_id.release_pr_ids.repository_id.name == prod.name
    release_id = to_pr(env, release)
    w_id.release_pr_ids.pr_id = release_id.id

    assert not w_id.errors
    w_id.action_freeze()

    # re-enable forward-port cron after freeze
    _, cron_id = env['ir.model.data'].check_object_reference('forwardport', 'port_forward', context={'active_test': False})
    env['ir.cron'].browse([cron_id]).active = True

    # run crons to process the feedback, run a second time in case of e.g.
    # forward porting
    env.run_crons()
    env.run_crons()

    assert release_id.state == 'merged'
    assert not env['runbot_merge.pull_requests'].search([
        ('state', '!=', 'merged')
    ]), "the release PRs should not be forward-ported"

def test_missing_magic_ref(env, config, make_repo):
    """There are cases where github fails to create / publish or fails to update
    the magic refs in refs/pull/*.

    In that case, pulling from the regular remote does not bring in the contents
    of the PR we're trying to forward port, and the forward porting process
    fails.

    Emulate this behaviour by updating the PR with a commit which lives in the
    repo but has no ref.
    """
    _, prod, _ = make_basic(env, config, make_repo, fp_token=True, fp_remote=True)
    a_head = prod.commit('refs/heads/a')
    with prod:
        [c] = prod.make_commits(a_head.id, Commit('x', tree={'x': '0'}), ref='heads/change')
        pr = prod.make_pr(target='a', head='change')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    # create variant of pr head in fork, update PR with that commit as head so
    # it's not found after a fetch, simulating an outdated or missing magic ref
    pr_id = to_pr(env, pr)
    assert pr_id.staging_id

    pr_id.head = '0'*40
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()

    assert not pr_id.staging_id
    assert pr_id.state == 'merged'

    # check that the fw failed
    assert not env['runbot_merge.pull_requests'].search([('source_id', '=', pr_id.id)]),\
        "forward port should not have been created"
    # check that the batch is still here and targeted for the future
    req = env['forwardport.batches'].search([])
    assert len(req) == 1
    assert req.retry_after > datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    # reset retry_after
    req.retry_after = '1900-01-01 01:01:01'

    # add a real commit
    with prod:
        [c2] = prod.make_commits(a_head.id, Commit('y', tree={'x': '0'}))
    assert c2 != c
    pr_id.head = c2
    env.run_crons()

    fp_id = env['runbot_merge.pull_requests'].search([('source_id', '=', pr_id.id)])
    assert fp_id
    # the cherrypick process fetches the commits on the PR in order to know
    # what they are (rather than e.g. diff the HEAD it branch with the target)
    # as a result it doesn't forwardport our fake, we'd have to reset the PR's
    # branch for that to happen
