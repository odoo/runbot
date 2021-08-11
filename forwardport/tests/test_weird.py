# -*- coding: utf-8 -*-
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
            'branch_ids': [
                (0, 0, {'name': 'a', 'fp_sequence': 2, 'fp_target': True}),
                (0, 0, {'name': 'b', 'fp_sequence': 1, 'fp_target': True}),
                (0, 0, {'name': 'c', 'fp_sequence': 0, 'fp_target': True}),
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
            (users['user'], "This pull request can not be forward ported: next "
                            "branch is 'b' but linked pull request %s#%d has a"
                            " next branch 'c'." % (b.name, pr_b.number)
            )
        ]
        assert pr_b.comments == [
            (users['reviewer'], 'hansen r+'),
            seen(env, pr_b, users),
            (users['user'], "This pull request can not be forward ported: next "
                            "branch is 'c' but linked pull request %s#%d has a"
                            " next branch 'b'." % (a.name, pr_a.number)
            )
        ]

def test_new_intermediate_branch(env, config, make_repo):
    """ In the case of a freeze / release a new intermediate branch appears in
    the sequence. New or ongoing forward ports should pick it up just fine (as
    the "next target" is decided when a PR is ported forward) however this is
    an issue for existing yet-to-be-merged sequences e.g. given the branches
    1.0, 2.0 and master, if a branch 3.0 is forked off from master and inserted
    before it, we need to create a new *intermediate* forward port PR
    """
    def validate(commit):
        prod.post_status(commit, 'success', 'ci/runbot')
        prod.post_status(commit, 'success', 'legal/cla')
    project, prod, _ = make_basic(env, config, make_repo, fp_token=True, fp_remote=True)
    original_c_tree = prod.read_tree(prod.commit('c'))
    prs = []
    with prod:
        for i in ['0', '1', '2']:
            prod.make_commits('a', Commit(i, tree={i:i}), ref='heads/branch%s' % i)
            pr = prod.make_pr(target='a', head='branch%s' % i)
            prs.append(pr)
            validate(pr.head)
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
        # cancel validation of PR2
        prod.post_status(prs[2].head, 'failure', 'ci/runbot')
        # also add a PR targeting b forward-ported to c, in order to check
        # for an insertion right after the source
        prod.make_commits('b', Commit('x', tree={'x': 'x'}), ref='heads/branchx')
        prx = prod.make_pr(target='b', head='branchx')
        validate(prx.head)
        prx.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        validate('staging.a')
        validate('staging.b')
    env.run_crons()

    # should have merged pr1, pr2 and prx and created their forward ports, now
    # validate pr0's FP so the c-targeted FP is created
    PRs = env['runbot_merge.pull_requests']
    pr0_id = PRs.search([
        ('repository.name', '=', prod.name),
        ('number', '=', prs[0].number),
    ])
    pr0_fp_id = PRs.search([
        ('source_id', '=', pr0_id.id),
    ])
    assert pr0_fp_id
    assert pr0_fp_id.target.name == 'b'
    with prod:
        validate(pr0_fp_id.head)
    env.run_crons()
    original0 = PRs.search([('parent_id', '=', pr0_fp_id.id)])
    assert original0, "Could not find FP of PR0 to C"
    assert original0.target.name == 'c'

    # also check prx's fp
    prx_id = PRs.search([('repository.name', '=', prod.name), ('number', '=', prx.number)])
    prx_fp_id = PRs.search([('source_id', '=', prx_id.id)])
    assert prx_fp_id
    assert prx_fp_id.target.name == 'c'

    # NOTE: the branch must be created on git(hub) first, probably
    # create new branch forked from the "current master" (c)
    c = prod.commit('c').id
    with prod:
        prod.make_ref('heads/new', c)
    currents = {branch.name: branch.id for branch in project.branch_ids}
    # insert a branch between "b" and "c"
    project.write({
        'branch_ids': [
            (1, currents['a'], {'fp_sequence': 3}),
            (1, currents['b'], {'fp_sequence': 2}),
            (0, False, {'name': 'new', 'fp_sequence': 1, 'fp_target': True}),
            (1, currents['c'], {'fp_sequence': 0})
        ]
    })
    env.run_crons()
    descendants = PRs.search([('source_id', '=', pr0_id.id)])
    new0 = descendants - pr0_fp_id - original0
    assert len(new0) == 1
    assert new0.parent_id == pr0_fp_id
    assert original0.parent_id == new0

    descx = PRs.search([('source_id', '=', prx_id.id)])
    newx = descx - prx_fp_id
    assert len(newx) == 1
    assert newx.parent_id == prx_id
    assert prx_fp_id.parent_id == newx

    # finish up: merge pr1 and pr2, ensure all the content is present in both
    # "new" (the newly inserted branch) and "c" (the tippity tip)
    with prod: # validate pr2
        prod.post_status(prs[2].head, 'success', 'ci/runbot')
    env.run_crons()
    # merge pr2
    with prod:
        validate('staging.a')
    env.run_crons()
    # ci on pr1/pr2 fp to b
    sources = [
        env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', prod.name),
            ('number', '=', pr.number),
        ]).id
        for pr in prs
    ]
    sources.append(prx_id.id)
    # CI all the forward port PRs (shouldn't hurt to re-ci the forward port of
    # prs[0] to b aka pr0_fp_id
    for target in ['b', 'new', 'c']:
        fps = PRs.search([('source_id', 'in', sources), ('target.name', '=', target)])
        with prod:
            for fp in fps:
                validate(fp.head)
        env.run_crons()
    # now fps should be the last PR of each sequence, and thus r+-able
    with prod:
        for pr in fps:
            assert pr.target.name == 'c'
            prod.get_pr(pr.number).post_comment(
                '%s r+' % project.fp_github_name,
                config['role_reviewer']['token'])
    assert all(p.state == 'merged' for p in PRs.browse(sources)), \
        "all sources should be merged"
    assert all(p.state == 'ready' for p in PRs.search([('id', 'not in', sources)])),\
        "All PRs except sources should be ready"
    env.run_crons()
    with prod:
        for target in ['b', 'new', 'c']:
            validate('staging.' + target)
    env.run_crons()
    assert all(p.state == 'merged' for p in PRs.search([])), \
        "All PRs should be merged now"

    assert prod.read_tree(prod.commit('c')) == {
        **original_c_tree,
        '0': '0', '1': '1', '2': '2', # updates from PRs
        'x': 'x',
    }, "check that C got all the updates"
    assert prod.read_tree(prod.commit('new')) == {
        **original_c_tree,
        '0': '0', '1': '1', '2': '2', # updates from PRs
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
    # user can't close PR directly
    with prod, pytest.raises(Exception):
        pr1.close(other_token) # what the fuck?
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
        (users['user'], f"I'm sorry, @{users['reviewer']}. Draft PRs can not be approved."),
    ]

    with prod:
        pr.draft = False
    assert pr.draft is False
    with prod:
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    assert pr_id.state == 'approved'
