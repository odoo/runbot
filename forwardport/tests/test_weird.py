# -*- coding: utf-8 -*-
import sys

import pytest
import re

from utils import *

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
            'branch_ids': [
                (0, 0, {'name': 'a', 'fp_sequence': 2, 'fp_target': True}),
                (0, 0, {'name': 'b', 'fp_sequence': 1, 'fp_target': True}),
                (0, 0, {'name': 'c', 'fp_sequence': 0, 'fp_target': True}),
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
    project.write({
        'repo_ids': [(0, 0, {
            'name': prod.name,
            'required_statuses': 'legal/cla,ci/runbot',
            'fp_remote_target': fp_remote and other.name,
        })],
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
    pr2 = prod.get_pr(pr2_id.number)
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
    assert not pr3_id.batch_ids

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
    def repos(self, env, config, make_repo):
        project = env['runbot_merge.project'].create({
            'name': 'proj',
            'github_token': config['github']['token'],
            'github_prefix': 'hansen',
            'fp_github_token': config['github']['token'],
            'branch_ids': [
                (0, 0, {'name': 'a', 'fp_sequence': 2, 'fp_target': True}),
                (0, 0, {'name': 'b', 'fp_sequence': 1, 'fp_target': True}),
                (0, 0, {'name': 'c', 'fp_sequence': 0, 'fp_target': True}),
            ]
        })

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
        project.write({
            'repo_ids': [
                (0, 0, {
                    'name': a.name,
                    'required_statuses': 'ci/runbot',
                    'fp_remote_target': a_dev.name,
                }),
                (0, 0, {
                    'name': b.name,
                    'required_statuses': 'ci/runbot',
                    'fp_remote_target': b_dev.name,
                    'branch_filter': '[("name", "in", ["a", "c"])]',
                })
            ]
        })
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
            (users['user'], "This pull request can not be forward ported: next "
                            "branch is 'b' but linked pull request %s#%d has a"
                            " next branch 'c'." % (b.name, pr_b.number)
            )
        ]
        assert pr_b.comments == [
            (users['reviewer'], 'hansen r+'),
            (users['user'], "This pull request can not be forward ported: next "
                            "branch is 'c' but linked pull request %s#%d has a"
                            " next branch 'b'." % (a.name, pr_a.number)
            )
        ]
