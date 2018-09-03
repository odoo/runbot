import datetime

import pytest

import odoo

@pytest.fixture
def repo(make_repo):
    return make_repo('repo')

def test_trivial_flow(env, repo):
    # create base branch
    m = repo.make_commit(None, "initial", None, tree={'a': 'some content'})
    repo.make_ref('heads/master', m)

    # create PR with 2 commits
    c0 = repo.make_commit(m, 'replace file contents', None, tree={'a': 'some other content'})
    c1 = repo.make_commit(c0, 'add file', None, tree={'a': 'some other content', 'b': 'a second file'})
    pr1 = repo.make_pr("gibberish", "blahblah", target='master', ctid=c1, user='user')

    [pr] = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', pr1.number),
    ])
    assert pr.state == 'opened'
    env['runbot_merge.project']._check_progress()
    assert pr1.labels == {'seen 🙂'}
    # nothing happened

    repo.post_status(c1, 'success', 'legal/cla')
    repo.post_status(c1, 'success', 'ci/runbot')
    assert pr.state == 'validated'
    env['runbot_merge.project']._check_progress()
    assert pr1.labels == {'seen 🙂', 'CI 🤖'}

    pr1.post_comment('hansen r+', 'reviewer')
    assert pr.state == 'ready'

    # can't check labels here as running the cron will stage it

    env['runbot_merge.project']._check_progress()
    assert pr.staging_id
    assert pr1.labels == {'seen 🙂', 'CI 🤖', 'r+ 👌', 'merging 👷'}

    # get head of staging branch
    staging_head = repo.commit('heads/staging.master')
    repo.post_status(staging_head.id, 'success', 'ci/runbot')
    repo.post_status(staging_head.id, 'success', 'legal/cla')

    env['runbot_merge.project']._check_progress()
    assert pr.state == 'merged'
    assert pr1.labels == {'seen 🙂', 'CI 🤖', 'r+ 👌', 'merged 🎉'}

    master = repo.commit('heads/master')
    # with default-rebase, only one parent is "known"
    assert master.parents[0] == m
    assert repo.read_tree(master) == {
        'a': b'some other content',
        'b': b'a second file',
    }
    assert master.message, "gibberish\n\nblahblah\n\ncloses odoo/odoo#1"

class TestWebhookSecurity:
    def test_no_secret(self, env, project, repo):
        """ Test 1: didn't add a secret to the repo, should be ignored
        """
        project.secret = "a secret"

        m = repo.make_commit(None, "initial", None, tree={'a': 'some content'})
        repo.make_ref('heads/master', m)

        c0 = repo.make_commit(m, 'replace file contents', None, tree={'a': 'some other content'})
        pr0 = repo.make_pr("gibberish", "blahblah", target='master', ctid=c0, user='user')

        assert not env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', pr0.number),
        ])

    def test_wrong_secret(self, env, project, repo):
        repo.set_secret("wrong secret")
        project.secret = "a secret"

        m = repo.make_commit(None, "initial", None, tree={'a': 'some content'})
        repo.make_ref('heads/master', m)

        c0 = repo.make_commit(m, 'replace file contents', None, tree={'a': 'some other content'})
        pr0 = repo.make_pr("gibberish", "blahblah", target='master', ctid=c0, user='user')

        assert not env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', pr0.number),
        ])

    def test_correct_secret(self, env, project, repo):
        repo.set_secret("a secret")
        project.secret = "a secret"

        m = repo.make_commit(None, "initial", None, tree={'a': 'some content'})
        repo.make_ref('heads/master', m)

        c0 = repo.make_commit(m, 'replace file contents', None, tree={'a': 'some other content'})
        pr0 = repo.make_pr("gibberish", "blahblah", target='master', ctid=c0, user='user')

        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', pr0.number),
        ])

def test_staging_conflict(env, repo):
    # create base branch
    m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
    repo.make_ref('heads/master', m)

    # create PR
    c0 = repo.make_commit(m, 'replace file contents', None, tree={'a': 'some other content'})
    c1 = repo.make_commit(c0, 'add file', None, tree={'a': 'some other content', 'b': 'a second file'})
    pr1 = repo.make_pr("gibberish", "blahblah", target='master', ctid=c1, user='user')
    repo.post_status(c1, 'success', 'legal/cla')
    repo.post_status(c1, 'success', 'ci/runbot')
    pr1.post_comment("hansen r+", "reviewer")
    env['runbot_merge.project']._check_progress()
    pr1 = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', 1)
    ])
    assert pr1.staging_id

    # create second PR and make ready for staging
    c2 = repo.make_commit(m, 'other', None, tree={'a': 'some content', 'c': 'ccc'})
    c3 = repo.make_commit(c2, 'other', None, tree={'a': 'some content', 'c': 'ccc', 'd': 'ddd'})
    pr2 = repo.make_pr('gibberish', 'blahblah', target='master', ctid=c3, user='user')
    repo.post_status(c3, 'success', 'legal/cla')
    repo.post_status(c3, 'success', 'ci/runbot')
    pr2.post_comment('hansen r+', "reviewer")
    env['runbot_merge.project']._check_progress()
    p_2 = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', pr2.number)
    ])
    assert p_2.state == 'ready', "PR2 should not have been staged since there is a pending staging for master"
    assert pr2.labels == {'seen 🙂', 'CI 🤖', 'r+ 👌'}

    staging_head = repo.commit('heads/staging.master')
    repo.post_status(staging_head.id, 'success', 'ci/runbot')
    repo.post_status(staging_head.id, 'success', 'legal/cla')
    env['runbot_merge.project']._check_progress()
    assert pr1.state == 'merged'
    assert p_2.staging_id

    staging_head = repo.commit('heads/staging.master')
    repo.post_status(staging_head.id, 'success', 'ci/runbot')
    repo.post_status(staging_head.id, 'success', 'legal/cla')
    env['runbot_merge.project']._check_progress()
    assert p_2.state == 'merged'

def test_staging_concurrent(env, repo):
    """ test staging to different targets, should be picked up together """
    m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
    repo.make_ref('heads/1.0', m)
    repo.make_ref('heads/2.0', m)

    env['runbot_merge.project'].search([]).write({
        'branch_ids': [(0, 0, {'name': '1.0'}), (0, 0, {'name': '2.0'})],
    })

    c10 = repo.make_commit(m, 'AAA', None, tree={'m': 'm', 'a': 'a'})
    c11 = repo.make_commit(c10, 'BBB', None, tree={'m': 'm', 'a': 'a', 'b': 'b'})
    pr1 = repo.make_pr('t1', 'b1', target='1.0', ctid=c11, user='user')
    repo.post_status(pr1.head, 'success', 'ci/runbot')
    repo.post_status(pr1.head, 'success', 'legal/cla')
    pr1.post_comment('hansen r+', "reviewer")

    c20 = repo.make_commit(m, 'CCC', None, tree={'m': 'm', 'c': 'c'})
    c21 = repo.make_commit(c20, 'DDD', None, tree={'m': 'm', 'c': 'c', 'd': 'd'})
    pr2 = repo.make_pr('t2', 'b2', target='2.0', ctid=c21, user='user')
    repo.post_status(pr2.head, 'success', 'ci/runbot')
    repo.post_status(pr2.head, 'success', 'legal/cla')
    pr2.post_comment('hansen r+', "reviewer")

    env['runbot_merge.project']._check_progress()
    pr1 = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', pr1.number)
    ])
    assert pr1.staging_id
    pr2 = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', pr2.number)
    ])
    assert pr2.staging_id

def test_staging_merge_fail(env, repo, users):
    """ # of staging failure (no CI) before mark & notify?
    """
    m1 = repo.make_commit(None, 'initial', None, tree={'f': 'm1'})
    m2 = repo.make_commit(m1, 'second', None, tree={'f': 'm2'})
    repo.make_ref('heads/master', m2)

    c1 = repo.make_commit(m1, 'other second', None, tree={'f': 'c1'})
    c2 = repo.make_commit(c1, 'third', None, tree={'f': 'c2'})
    prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
    repo.post_status(prx.head, 'success', 'ci/runbot')
    repo.post_status(prx.head, 'success', 'legal/cla')
    prx.post_comment('hansen r+', "reviewer")

    env['runbot_merge.project']._check_progress()
    pr1 = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', prx.number)
    ])
    assert pr1.state == 'error'
    assert prx.labels == {'seen 🙂', 'error 🙅'}
    assert prx.comments == [
        (users['reviewer'], 'hansen r+'),
        (users['user'], 'Unable to stage PR (merge conflict)'),
    ]

def test_staging_ci_timeout(env, repo, users):
    """If a staging timeouts (~ delay since staged greater than
    configured)... requeue?
    """
    m = repo.make_commit(None, 'initial', None, tree={'f': 'm'})
    repo.make_ref('heads/master', m)

    c1 = repo.make_commit(m, 'first', None, tree={'f': 'c1'})
    c2 = repo.make_commit(c1, 'second', None, tree={'f': 'c2'})
    prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
    repo.post_status(prx.head, 'success', 'ci/runbot')
    repo.post_status(prx.head, 'success', 'legal/cla')
    prx.post_comment('hansen r+', "reviewer")
    env['runbot_merge.project']._check_progress()

    pr1 = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', prx.number)
    ])
    assert pr1.staging_id
    timeout = env['runbot_merge.project'].search([]).ci_timeout

    pr1.staging_id.staged_at = odoo.fields.Datetime.to_string(datetime.datetime.now() - datetime.timedelta(minutes=2*timeout))
    env['runbot_merge.project']._check_progress()
    assert pr1.state == 'error', "timeout should fail the PR"

def test_staging_ci_failure_single(env, repo, users):
    """ on failure of single-PR staging, mark & notify failure
    """
    m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
    repo.make_ref('heads/master', m)

    c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
    c2 = repo.make_commit(c1, 'second', None, tree={'m': 'c2'})
    prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
    repo.post_status(prx.head, 'success', 'ci/runbot')
    repo.post_status(prx.head, 'success', 'legal/cla')
    prx.post_comment('hansen r+', "reviewer")
    env['runbot_merge.project']._check_progress()
    assert env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', prx.number)
    ]).staging_id

    staging_head = repo.commit('heads/staging.master')
    repo.post_status(staging_head.id, 'success', 'legal/cla')
    repo.post_status(staging_head.id, 'failure', 'ci/runbot') # stable genius
    env['runbot_merge.project']._check_progress()
    assert env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', prx.number)
    ]).state == 'error'

    assert prx.comments == [
        (users['reviewer'], 'hansen r+'),
        (users['user'], 'Staging failed: ci/runbot')
    ]

def test_ff_failure(env, repo, users):
    """ target updated while the PR is being staged => redo staging """
    m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
    repo.make_ref('heads/master', m)

    c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
    c2 = repo.make_commit(c1, 'second', None, tree={'m': 'c2'})
    prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
    repo.post_status(prx.head, 'success', 'legal/cla')
    repo.post_status(prx.head, 'success', 'ci/runbot')
    prx.post_comment('hansen r+', "reviewer")
    env['runbot_merge.project']._check_progress()
    assert env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', prx.number)
    ]).staging_id

    m2 = repo.make_commit('heads/master', 'cockblock', None, tree={'m': 'm', 'm2': 'm2'})
    assert repo.commit('heads/master').id == m2

    # report staging success & run cron to merge
    staging = repo.commit('heads/staging.master')
    repo.post_status(staging.id, 'success', 'legal/cla')
    repo.post_status(staging.id, 'success', 'ci/runbot')
    env['runbot_merge.project']._check_progress()

    assert env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', prx.number)
    ]).staging_id, "merge should not have succeeded"
    assert repo.commit('heads/staging.master').id != staging.id,\
        "PR should be staged to a new commit"

def test_edit(env, repo):
    """ Editing PR:

    * title (-> message)
    * body (-> message)
    * base.ref (-> target)
    """
    branch_1 = env['runbot_merge.branch'].create({
        'name': '1.0',
        'project_id': env['runbot_merge.project'].search([]).id,
    })

    m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
    repo.make_ref('heads/master', m)
    repo.make_ref('heads/1.0', m)
    repo.make_ref('heads/2.0', m)

    c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
    c2 = repo.make_commit(c1, 'second', None, tree={'m': 'c2'})
    prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
    pr = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', prx.number)
    ])
    assert pr.message == 'title\n\nbody'
    prx.title = "title 2"
    assert pr.message == 'title 2\n\nbody'
    prx.base = '1.0'
    assert pr.target == branch_1
    # FIXME: should a PR retargeted to an unmanaged branch really be deleted?
    prx.base = '2.0'
    assert not pr.exists()
    env['runbot_merge.project']._check_progress()
    assert prx.labels == set()

    prx.base = '1.0'
    assert env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', prx.number)
    ]).target == branch_1

@pytest.mark.skip(reason="what do?")
def test_edit_retarget_managed(env, repo):
    """ A PR targeted to an un-managed branch is ignored but if the PR
    is re-targeted to a managed branch it should be managed

    TODO: maybe bot should tag PR as managed/unmanaged?
    """
@pytest.mark.skip(reason="What do?")
def test_edit_staged(env, repo):
    """
    What should happen when editing the PR/metadata (not pushing) of a staged PR
    """
def test_close_staged(env, repo):
    """
    When closing a staged PR, cancel the staging
    """
    m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
    repo.make_ref('heads/master', m)

    c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
    prx = repo.make_pr('title', 'body', target='master', ctid=c, user='user')
    repo.post_status(prx.head, 'success', 'legal/cla')
    repo.post_status(prx.head, 'success', 'ci/runbot')
    prx.post_comment('hansen r+', user='reviewer')
    pr = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', prx.number),
    ])
    env['runbot_merge.project']._check_progress()
    assert pr.state == 'ready'
    assert pr.staging_id

    prx.close()

    assert not pr.staging_id
    assert not env['runbot_merge.stagings'].search([])

class TestRetry:
    @pytest.mark.xfail(reason="This may not be a good idea as it could lead to tons of rebuild spam")
    def test_auto_retry_push(self, env, repo):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        c2 = repo.make_commit(c1, 'second', None, tree={'m': 'c2'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')
        prx.post_comment('hansen r+', "reviewer")
        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).staging_id

        staging_head = repo.commit('heads/staging.master')
        repo.post_status(staging_head.id, 'success', 'legal/cla')
        repo.post_status(staging_head.id, 'failure', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])
        assert pr.state == 'error'

        prx.push(repo.make_commit(c2, 'third', None, tree={'m': 'c3'}))
        assert pr.state == 'approved'
        env['runbot_merge.project']._check_progress()
        assert pr.state == 'approved'
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')
        env['runbot_merge.project']._check_progress()
        assert pr.state == 'ready'

        staging_head2 = repo.commit('heads/staging.master')
        assert staging_head2 != staging_head
        repo.post_status(staging_head2.id, 'success', 'legal/cla')
        repo.post_status(staging_head2.id, 'success', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        assert pr.state == 'merged'

    @pytest.mark.parametrize('retrier', ['user', 'other', 'reviewer'])
    def test_retry_comment(self, env, repo, retrier, users):
        """ An accepted but failed PR should be re-tried when the author or a
        reviewer asks for it
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        c2 = repo.make_commit(c1, 'second', None, tree={'m': 'c2'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')
        prx.post_comment('hansen r+ delegate=%s' % users['other'], "reviewer")
        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).staging_id

        staging_head = repo.commit('heads/staging.master')
        repo.post_status(staging_head.id, 'success', 'legal/cla')
        repo.post_status(staging_head.id, 'failure', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'error'

        prx.post_comment('hansen retry', retrier)
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'ready'
        env['runbot_merge.project']._check_progress()

        staging_head2 = repo.commit('heads/staging.master')
        assert staging_head2 != staging_head
        repo.post_status(staging_head2.id, 'success', 'legal/cla')
        repo.post_status(staging_head2.id, 'success', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'merged'

    @pytest.mark.parametrize('disabler', ['user', 'other', 'reviewer'])
    def test_retry_disable(self, env, repo, disabler, users):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        c2 = repo.make_commit(c1, 'second', None, tree={'m': 'c2'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')
        prx.post_comment('hansen r+ delegate=%s' % users['other'], "reviewer")
        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).staging_id

        staging_head = repo.commit('heads/staging.master')
        repo.post_status(staging_head.id, 'success', 'legal/cla')
        repo.post_status(staging_head.id, 'failure', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])
        assert pr.state == 'error'

        prx.post_comment('hansen r-', user=disabler)
        assert pr.state == 'validated'
        prx.push(repo.make_commit(c2, 'third', None, tree={'m': 'c3'}))
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')
        env['runbot_merge.project']._check_progress()
        assert pr.state == 'validated'

class TestMergeMethod:
    """
    if event['pull_request']['commits'] == 1, "squash" (/rebase); otherwise
    regular merge
    """
    def test_staging_merge_squash(self, repo, env):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
        repo.make_ref('heads/master', m2)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c1, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+', "reviewer")
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).squash

        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).staging_id

        staging = repo.commit('heads/staging.master')
        assert not repo.is_ancestor(prx.head, of=staging.id),\
            "the pr head should not be an ancestor of the staging branch in a squash merge"
        assert staging.parents == [m2],\
            "the previous master's tip should be the sole parent of the staging commit"
        assert repo.read_tree(staging) == {
            'm': b'c1', 'm2': b'm2',
        }, "the tree should still be correctly merged"

        repo.post_status(staging.id, 'success', 'legal/cla')
        repo.post_status(staging.id, 'success', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'merged'
        assert prx.state == 'closed'

    def test_pr_update_unsquash(self, repo, env):
        """
        If a PR starts with 1 commit and a second commit is added, the PR
        should be unflagged as squash
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
        repo.make_ref('heads/master', m2)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c1, user='user')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.squash, "a PR with a single commit should be squashed"

        prx.push(repo.make_commit(c1, 'second2', None, tree={'m': 'c2'}))
        assert not pr.squash, "a PR with a single commit should not be squashed"

    def test_pr_reset_squash(self, repo, env):
        """
        If a PR starts at >1 commits and is reset back to 1, the PR should be
        re-flagged as squash
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
        repo.make_ref('heads/master', m2)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        c2 = repo.make_commit(c1, 'second2', None, tree={'m': 'c2'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert not pr.squash, "a PR with a single commit should not be squashed"

        prx.push(repo.make_commit(m, 'fixup', None, tree={'m': 'c2'}))
        assert pr.squash, "a PR with a single commit should be squashed"

    def test_pr_rebase_merge(self, repo, env):
        """ a multi-commit PR should be rebased & merged by default

        left: PR
        right: post-merge result

                     +------+                   +------+
                     |  M0  |                   |  M0  |
                     +--^---+                   +--^---+
                        |                          |
                        |                          |
                     +--+---+                   +--+---+
                +---->  M1  <--+                |  M1  <--+
                |    +------+  |                +------+  |
                |              |                          |
                |              |                          |
             +--+---+      +---+---+    +------+      +---+---+
             |  B0  |      |  M2   |    |  B0  +------>  M2   |
             +--^---+      +-------+    +--^---+      +---^---+
                |                          |              |
             +--+---+                   +--+---+          |
          PR |  B1  |                   |  B1  |          |
             +------+                   +--^---+          |
                                           |          +---+---+
                                           +----------+ merge |
                                                      +-------+
        """
        m0 = repo.make_commit(None, 'M0', None, tree={'m': '0'})
        m1 = repo.make_commit(m0, 'M1', None, tree={'m': '1'})
        m2 = repo.make_commit(m1, 'M2', None, tree={'m': '2'})
        repo.make_ref('heads/master', m2)

        b0 = repo.make_commit(m1, 'B0', None, tree={'m': '1', 'b': '0'})
        b1 = repo.make_commit(b0, 'B1', None, tree={'m': '1', 'b': '1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=b1, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+', "reviewer")

        env['runbot_merge.project']._check_progress()

        # create a dag (msg:str, parents:set) from the log
        staging = log_to_node(repo.log('heads/staging.master'))
        # then compare to the dag version of the right graph
        nm2 = node('M2', node('M1', node('M0')))
        nb1 = node('B1', node('B0', nm2))
        expected = (
            'title\n\nbody\n\ncloses {}#{}'.format(repo.name, prx.number),
            frozenset([nm2, nb1])
        )
        assert staging == expected

        final_tree = repo.read_tree(repo.commit('heads/staging.master'))
        assert final_tree == {'m': b'2', 'b': b'1'}, "sanity check of final tree"

    @pytest.mark.skip(reason="what do if the PR contains merge commits???")
    def test_pr_contains_merges(self, repo, env):
        pass

    def test_pr_unrebase(self, repo, env):
        """ should be possible to flag a PR as regular-merged, regardless of
        its commits count

        M      M<--+
        ^      ^   |
        |  ->  |   C0
        +      |   ^
        C0     +   |
               gib-+
        """
        m = repo.make_commit(None, "M", None, tree={'a': 'a'})
        repo.make_ref('heads/master', m)

        c0 = repo.make_commit(m, 'C0', None, tree={'a': 'b'})
        prx = repo.make_pr("gibberish", "blahblah", target='master', ctid=c0, user='user')
        env['runbot_merge.project']._check_progress()

        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+ rebase-', 'reviewer')
        env['runbot_merge.project']._check_progress()

        repo.post_status('heads/staging.master', 'success', 'ci/runbot')
        repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env['runbot_merge.project']._check_progress()

        master = repo.commit('heads/master')
        assert master.parents == [m, prx.head], \
            "master's parents should be the old master & the PR head"

        m = node('M')
        c0 = node('C0', m)
        expected = node('gibberish\n\nblahblah\n\ncloses {}#{}'.format(repo.name, prx.number), m, c0)
        assert log_to_node(repo.log('heads/master')), expected

    def test_pr_mergehead(self, repo, env):
        """ if the head of the PR is a merge commit and one of the parents is
        in the target, replicate the merge commit instead of merging

        rankdir="BT"
        M2 -> M1
        C0 -> M1
        C1 -> C0
        C1 -> M2

        C1 [label = "\\N / MERGE"]
        """
        m1 = repo.make_commit(None, "M1", None, tree={'a': '0'})
        m2 = repo.make_commit(m1, "M2", None, tree={'a': '1'})
        repo.make_ref('heads/master', m2)

        c0 = repo.make_commit(m1, 'C0', None, tree={'a': '0', 'b': '2'})
        c1 = repo.make_commit([c0, m2], 'C1', None, tree={'a': '1', 'b': '2'})
        prx = repo.make_pr("T", "TT", target='master', ctid=c1, user='user')
        env['runbot_merge.project']._check_progress()

        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+ rebase-', 'reviewer')
        env['runbot_merge.project']._check_progress()

        repo.post_status('heads/staging.master', 'success', 'ci/runbot')
        repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env['runbot_merge.project']._check_progress()

        master = repo.commit('heads/master')
        assert master.parents == [m2, c0]
        m1 = node('M1')
        expected = node('C1', node('C0', m1), node('M2', m1))
        assert log_to_node(repo.log('heads/master')), expected

    def test_pr_mergehead_nonmember(self, repo, env):
        """ if the head of the PR is a merge commit but none of the parents is
        in the target, merge normally

        rankdir="BT"
        M2 -> M1
        B0 -> M1
        C0 -> M1
        C1 -> C0
        C1 -> B0

        MERGE -> M2
        MERGE -> C1
        """
        m1 = repo.make_commit(None, "M1", None, tree={'a': '0'})
        m2 = repo.make_commit(m1, "M2", None, tree={'a': '1'})
        repo.make_ref('heads/master', m2)

        b0 = repo.make_commit(m1, 'B0', None, tree={'a': '0', 'bb': 'bb'})

        c0 = repo.make_commit(m1, 'C0', None, tree={'a': '0', 'b': '2'})
        c1 = repo.make_commit([c0, b0], 'C1', None, tree={'a': '0', 'b': '2', 'bb': 'bb'})
        prx = repo.make_pr("T", "TT", target='master', ctid=c1, user='user')
        env['runbot_merge.project']._check_progress()

        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+ rebase-', 'reviewer')
        env['runbot_merge.project']._check_progress()

        repo.post_status('heads/staging.master', 'success', 'ci/runbot')
        repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env['runbot_merge.project']._check_progress()

        master = repo.commit('heads/master')
        assert master.parents == [m2, c1]
        assert repo.read_tree(master) == {'a': b'1', 'b': b'2', 'bb': b'bb'}

        m1 = node('M1')
        expected = node(
            'T\n\nTT\n\ncloses {}#{}'.format(repo.name, prx.number),
            node('M2', m1),
            node('C1', node('C0', m1), node('B0', m1))
        )
        assert log_to_node(repo.log('heads/master')), expected

    @pytest.mark.xfail(reason="removed support for squash+ command")
    def test_force_squash_merge(self, repo, env):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
        repo.make_ref('heads/master', m2)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        c2 = repo.make_commit(c1, 'second', None, tree={'m': 'c2'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+ squash+', "reviewer")
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).squash

        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).staging_id

        staging = repo.commit('heads/staging.master')
        assert not repo.is_ancestor(prx.head, of=staging.id),\
            "the pr head should not be an ancestor of the staging branch in a squash merge"
        assert staging.parents == [m2],\
            "the previous master's tip should be the sole parent of the staging commit"
        assert repo.read_tree(staging) == {
            'm': b'c2', 'm2': b'm2',
        }, "the tree should still be correctly merged"

        repo.post_status(staging.id, 'success', 'legal/cla')
        repo.post_status(staging.id, 'success', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'merged'
        assert prx.state == 'closed'

    @pytest.mark.xfail(reason="removed support for squash- command")
    def test_disable_squash_merge(self, repo, env):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
        repo.make_ref('heads/master', m2)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c1, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+ squash-', "reviewer")
        assert not env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).squash

        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).staging_id

        staging = repo.commit('heads/staging.master')
        assert repo.is_ancestor(prx.head, of=staging.id)
        assert staging.parents == [m2, c1]
        assert repo.read_tree(staging) == {
            'm': b'c1', 'm2': b'm2',
        }

        repo.post_status(staging.id, 'success', 'legal/cla')
        repo.post_status(staging.id, 'success', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'merged'
        assert prx.state == 'closed'

class TestPRUpdate(object):
    """ Pushing on a PR should update the HEAD except for merged PRs, it
    can have additional effect (see individual tests)
    """
    def test_update_opened(self, env, repo):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c, user='user')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.head == c
        # alter & push force PR entirely
        c2 = repo.make_commit(m, 'first', None, tree={'m': 'cc'})
        prx.push(c2)
        assert pr.head == c2

    def test_reopen_update(self, env, repo):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c, user='user')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        prx.close()
        assert pr.state == 'closed'
        assert pr.head == c

        prx.open()
        assert pr.state == 'opened'

        c2 = repo.make_commit(c, 'first', None, tree={'m': 'cc'})
        prx.push(c2)
        assert pr.head == c2

    def test_update_validated(self, env, repo):
        """ Should reset to opened
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.head == c
        assert pr.state == 'validated'

        c2 = repo.make_commit(m, 'first', None, tree={'m': 'cc'})
        prx.push(c2)
        assert pr.head == c2
        assert pr.state == 'opened'

    def test_update_approved(self, env, repo):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c, user='user')
        prx.post_comment('hansen r+', user='reviewer')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.head == c
        assert pr.state == 'approved'

        c2 = repo.make_commit(c, 'first', None, tree={'m': 'cc'})
        prx.push(c2)
        assert pr.head == c2
        assert pr.state == 'opened'

    def test_update_ready(self, env, repo):
        """ Should reset to opened
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+', user='reviewer')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.head == c
        assert pr.state == 'ready'

        c2 = repo.make_commit(c, 'first', None, tree={'m': 'cc'})
        prx.push(c2)
        assert pr.head == c2
        assert pr.state == 'opened'

    def test_update_staged(self, env, repo):
        """ Should cancel the staging & reset PR to opened
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+', user='reviewer')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        env['runbot_merge.project']._check_progress()
        assert pr.state == 'ready'
        assert pr.staging_id

        c2 = repo.make_commit(c, 'first', None, tree={'m': 'cc'})
        prx.push(c2)
        assert pr.head == c2
        assert pr.state == 'opened'
        assert not pr.staging_id
        assert not env['runbot_merge.stagings'].search([])

    def test_update_error(self, env, repo):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+', user='reviewer')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        env['runbot_merge.project']._check_progress()
        assert pr.state == 'ready'
        assert pr.staging_id

        h = repo.commit('heads/staging.master').id
        repo.post_status(h, 'success', 'legal/cla')
        repo.post_status(h, 'failure', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        assert not pr.staging_id
        assert pr.state == 'error'

        c2 = repo.make_commit(c, 'first', None, tree={'m': 'cc'})
        prx.push(c2)
        assert pr.head == c2
        assert pr.state == 'error'

    def test_unknown_pr(self, env, repo):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/1.0', m)

        c = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='1.0', ctid=c, user='user')
        assert not env['runbot_merge.pull_requests'].search([('number', '=', prx.number)])

        env['runbot_merge.project'].search([]).write({
            'branch_ids': [(0, 0, {'name': '1.0'})]
        })

        c2 = repo.make_commit(c, 'second', None, tree={'m': 'c2'})
        prx.push(c2)

        assert not env['runbot_merge.pull_requests'].search([('number', '=', prx.number)])

class TestBatching(object):
    def _pr(self, repo, prefix, trees, *,
            target='master', user='user', reviewer='reviewer',
            statuses=(('ci/runbot', 'success'), ('legal/cla', 'success'))
        ):
        """ Helper creating a PR from a series of commits on a base

        :type repo: fake_github.Repo
        :param prefix: a prefix used for commit messages, PR title & PR body
        :param trees: a list of dicts symbolising the tree for the corresponding commit.
                      each tree is an update on the "current state" of the tree
        :param target: branch, both the base commit and the PR target
        :type target: str
        :type user: str
        :type reviewer: str | None
        :type statuses: List[(str, str)]
        """
        base = repo.commit('heads/{}'.format(target))
        tree = repo.read_tree(base)
        c = base.id
        for i, t in enumerate(trees):
            tree.update(t)
            c = repo.make_commit(c, 'commit_{}_{:02}'.format(prefix, i), None, tree=dict(tree))
        pr = repo.make_pr('title {}'.format(prefix), 'body {}'.format(prefix), target=target, ctid=c, user=user, label=prefix)

        for context, result in statuses:
            repo.post_status(c, result, context)
        if reviewer:
            pr.post_comment('hansen r+', reviewer)
        return pr

    def _get(self, env, repo, number):
        return env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', number),
        ])

    def test_staging_batch(self, env, repo):
        """ If multiple PRs are ready for the same target at the same point,
        they should be staged together
        """
        m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
        repo.make_ref('heads/master', m)

        pr1 = self._pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}])
        pr2 = self._pr(repo, 'PR2', [{'c': 'CCC'}, {'d': 'DDD'}])

        env['runbot_merge.project']._check_progress()
        pr1 = self._get(env, repo, pr1.number)
        assert pr1.staging_id
        pr2 = self._get(env, repo, pr2.number)
        assert pr1.staging_id
        assert pr2.staging_id
        assert pr1.staging_id == pr2.staging_id

    def test_batching_pressing(self, env, repo):
        """ "Pressing" PRs should be selected before normal & batched together
        """
        m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
        repo.make_ref('heads/master', m)

        pr21 = self._pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}])
        pr22 = self._pr(repo, 'PR2', [{'c': 'CCC'}, {'d': 'DDD'}])

        pr11 = self._pr(repo, 'Pressing1', [{'x': 'x'}, {'y': 'y'}])
        pr12 = self._pr(repo, 'Pressing2', [{'z': 'z'}, {'zz': 'zz'}])
        pr11.post_comment('hansen priority=1', 'reviewer')
        pr12.post_comment('hansen priority=1', 'reviewer')

        pr21, pr22, pr11, pr12 = prs = [self._get(env, repo, pr.number) for pr in [pr21, pr22, pr11, pr12]]
        assert pr21.priority == pr22.priority == 2
        assert pr11.priority == pr12.priority == 1

        env['runbot_merge.project']._check_progress()

        assert all(pr.state == 'ready' for pr in prs)
        assert not pr21.staging_id
        assert not pr22.staging_id
        assert pr11.staging_id
        assert pr12.staging_id
        assert pr11.staging_id == pr12.staging_id

    def test_batching_urgent(self, env, repo):
        m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
        repo.make_ref('heads/master', m)

        pr21 = self._pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}])
        pr22 = self._pr(repo, 'PR2', [{'c': 'CCC'}, {'d': 'DDD'}])

        pr11 = self._pr(repo, 'Pressing1', [{'x': 'x'}, {'y': 'y'}])
        pr12 = self._pr(repo, 'Pressing2', [{'z': 'z'}, {'zz': 'zz'}])
        pr11.post_comment('hansen priority=1', 'reviewer')
        pr12.post_comment('hansen priority=1', 'reviewer')

        # stage PR1
        env['runbot_merge.project']._check_progress()
        p_11, p_12, p_21, p_22 = \
            [self._get(env, repo, pr.number) for pr in [pr11, pr12, pr21, pr22]]
        assert not p_21.staging_id or p_22.staging_id
        assert p_11.staging_id and p_12.staging_id
        assert p_11.staging_id == p_12.staging_id
        staging_1 = p_11.staging_id

        # no statuses run on PR0s
        pr01 = self._pr(repo, 'Urgent1', [{'n': 'n'}, {'o': 'o'}], reviewer=None, statuses=[])
        pr01.post_comment('hansen priority=0', 'reviewer')
        p_01 = self._get(env, repo, pr01.number)
        assert p_01.state == 'opened'
        assert p_01.priority == 0

        env['runbot_merge.project']._check_progress()
        # first staging should be cancelled and PR0 should be staged
        # regardless of CI (or lack thereof)
        assert not staging_1.active
        assert not p_11.staging_id and not p_12.staging_id
        assert p_01.staging_id

    def test_batching_urgenter_than_split(self, env, repo):
        """ p=0 PRs should take priority over split stagings (processing
        of a staging having CI-failed and being split into sub-stagings)
        """
        m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
        repo.make_ref('heads/master', m)

        pr1 = self._pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}])
        p_1 = self._get(env, repo, pr1.number)
        pr2 = self._pr(repo, 'PR2', [{'a': 'some content', 'c': 'CCC'}, {'d': 'DDD'}])
        p_2 = self._get(env, repo, pr2.number)

        env['runbot_merge.project']._check_progress()
        st = env['runbot_merge.stagings'].search([])
        # both prs should be part of the staging
        assert st.mapped('batch_ids.prs') == p_1 | p_2
        # add CI failure
        repo.post_status('heads/staging.master', 'failure', 'ci/runbot')
        repo.post_status('heads/staging.master', 'success', 'legal/cla')

        env['runbot_merge.project']._check_progress()
        # should have staged the first half
        assert p_1.staging_id.heads
        assert not p_2.staging_id.heads

        # during restaging of pr1, create urgent PR
        pr0 = self._pr(repo, 'urgent', [{'a': 'a', 'b': 'b'}], reviewer=None, statuses=[])
        pr0.post_comment('hansen priority=0', 'reviewer')

        env['runbot_merge.project']._check_progress()
        # TODO: maybe just deactivate stagings instead of deleting them when canceling?
        assert not p_1.staging_id
        assert self._get(env, repo, pr0.number).staging_id

    def test_urgent_failed(self, env, repo):
        """ Ensure pr[p=0,state=failed] don't get picked up
        """
        m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
        repo.make_ref('heads/master', m)

        pr21 = self._pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}])

        p_21 = self._get(env, repo, pr21.number)

        # no statuses run on PR0s
        pr01 = self._pr(repo, 'Urgent1', [{'n': 'n'}, {'o': 'o'}], reviewer=None, statuses=[])
        pr01.post_comment('hansen priority=0', 'reviewer')
        p_01 = self._get(env, repo, pr01.number)
        p_01.state = 'error'

        env['runbot_merge.project']._check_progress()
        assert not p_01.staging_id, "p_01 should not be picked up as it's failed"
        assert p_21.staging_id, "p_21 should have been staged"

    @pytest.mark.skip(reason="Maybe nothing to do, the PR is just skipped and put in error?")
    def test_batching_merge_failure(self):
        pass

    def test_staging_ci_failure_batch(self, env, repo):
        """ on failure split batch & requeue
        """
        m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
        repo.make_ref('heads/master', m)

        pr1 = self._pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}])
        pr2 = self._pr(repo, 'PR2', [{'a': 'some content', 'c': 'CCC'}, {'d': 'DDD'}])

        env['runbot_merge.project']._check_progress()
        st = env['runbot_merge.stagings'].search([])
        # both prs should be part of the staging
        assert len(st.mapped('batch_ids.prs')) == 2
        # add CI failure
        repo.post_status('heads/staging.master', 'failure', 'ci/runbot')
        repo.post_status('heads/staging.master', 'success', 'legal/cla')

        pr1 = env['runbot_merge.pull_requests'].search([('number', '=', pr1.number)])
        pr2 = env['runbot_merge.pull_requests'].search([('number', '=', pr2.number)])

        env['runbot_merge.project']._check_progress()
        # should have split the existing batch into two, with one of the
        # splits having been immediately restaged
        st = env['runbot_merge.stagings'].search([])
        assert len(st) == 1
        assert pr1.staging_id and pr1.staging_id == st

        sp = env['runbot_merge.split'].search([])
        assert len(sp) == 1

        # This is the failing PR!
        h = repo.commit('heads/staging.master').id
        repo.post_status(h, 'failure', 'ci/runbot')
        repo.post_status(h, 'success', 'legal/cla')
        env['runbot_merge.project']._check_progress()
        assert pr1.state == 'error'

        assert pr2.staging_id

        h = repo.commit('heads/staging.master').id
        repo.post_status(h, 'success', 'ci/runbot')
        repo.post_status(h, 'success', 'legal/cla')
        env['runbot_merge.project']._check_progress()
        assert pr2.state == 'merged'

class TestReviewing(object):
    def test_reviewer_rights(self, env, repo):
        """Only users with review rights will have their r+ (and other
        attributes) taken in account
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
        repo.make_ref('heads/master', m2)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c1, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+', user='other')

        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'validated'
        prx.post_comment('hansen r+', user='reviewer')
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'ready'

    def test_self_review_fail(self, env, repo, users):
        """ Normal reviewers can't self-review
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
        repo.make_ref('heads/master', m2)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c1, user='reviewer')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+', user='reviewer')

        assert prx.user == users['reviewer']
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'validated'

    def test_self_review_success(self, env, repo, users):
        """ Some users are allowed to self-review
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
        repo.make_ref('heads/master', m2)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c1, user='self_reviewer')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+', user='self_reviewer')

        assert prx.user == users['self_reviewer']
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'ready'

    def test_delegate_review(self, env, repo, users):
        """Users should be able to delegate review to either the creator of
        the PR or an other user without review rights
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
        repo.make_ref('heads/master', m2)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c1, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen delegate+', user='reviewer')
        prx.post_comment('hansen r+', user='user')

        assert prx.user == users['user']
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'ready'

    def test_delegate_review_thirdparty(self, env, repo, users):
        """Users should be able to delegate review to either the creator of
        the PR or an other user without review rights
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
        repo.make_ref('heads/master', m2)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c1, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen delegate=%s' % users['other'], user='reviewer')
        prx.post_comment('hansen r+', user='user')

        assert prx.user == users['user']
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'validated'

        prx.post_comment('hansen r+', user='other')
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'ready'

    def test_actual_review(self, env, repo):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
        repo.make_ref('heads/master', m2)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c1, user='user')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])

        prx.post_review('COMMENT', 'reviewer', "hansen priority=1")
        assert pr.priority == 1
        assert pr.state == 'opened'

        prx.post_review('APPROVE', 'reviewer', "hansen priority=2")
        assert pr.priority == 2
        assert pr.state == 'approved'

class TestUnknownPR:
    """ Sync PRs initially looked excellent but aside from the v4 API not
    being stable yet, it seems to have greatly regressed in performances to
    the extent that it's almost impossible to sync odoo/odoo today: trying to
    fetch more than 2 PRs per query will fail semi-randomly at one point, so
    fetching all 15000 PRs takes hours

    => instead, create PRs on the fly when getting notifications related to
       valid but unknown PRs

    * get statuses if head commit unknown (additional cron?)
    * handle any comment & review (existing PRs may enter the system on a review/r+)
    """
    def test_rplus_unknown(self, repo, env):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
        repo.make_ref('heads/master', m2)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c1, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        # assume an unknown but ready PR: we don't know the PR or its head commit
        env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ]).unlink()
        env['runbot_merge.commit'].search([('sha', '=', prx.head)]).unlink()

        # reviewer reviewers
        prx.post_comment('hansen r+', "reviewer")

        Fetch = env['runbot_merge.fetch_job']
        assert Fetch.search([('repository', '=', repo.name), ('number', '=', prx.number)])
        env['runbot_merge.project']._check_fetch()
        assert not Fetch.search([('repository', '=', repo.name), ('number', '=', prx.number)])

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])
        assert pr.state == 'ready'

        env['runbot_merge.project']._check_progress()
        assert pr.staging_id

def node(name, *children):
    assert type(name) is str
    return name, frozenset(children)
def log_to_node(log):
    log = list(log)
    nodes = {}
    for c in reversed(log):
        nodes[c['sha']] = (c['commit']['message'], frozenset(
            nodes[p['sha']]
            for p in (c['parents'])
        ))
    return nodes[log[0]['sha']]
