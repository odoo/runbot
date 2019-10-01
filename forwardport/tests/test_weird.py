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
        project = env['runbot_merge.project'].create({
            'name': 'myproject',
            'github_token': config['github']['token'],
            'github_prefix': 'hansen',
            'fp_github_token': fp_token and config['github']['token'],
            'required_statuses': 'legal/cla,ci/runbot',
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
