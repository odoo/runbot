import datetime

import pytest

import odoo

from fake_github import git

@pytest.fixture
def repo(gh, env):
    env['res.partner'].create({
        'name': "Reviewer",
        'github_login': 'reviewer',
        'reviewer': True,
    })
    env['res.partner'].create({
        'name': "Self Reviewer",
        'github_login': 'self-reviewer',
        'self_reviewer': True,
    })
    env['res.partner'].create({
        'name': "Other",
        'github_login': 'other',
    })
    env['runbot_merge.project'].create({
        'name': 'odoo',
        'github_token': 'okokok',
        'github_prefix': 'hansen',
        'repo_ids': [(0, 0, {'name': 'odoo/odoo'})],
        'branch_ids': [(0, 0, {'name': 'master'})],
        'required_statuses': 'legal/cla,ci/runbot',
    })
    # need to create repo & branch in env so hook will allow processing them
    return gh.repo('odoo/odoo', hooks=[
        ((odoo.http.root, '/runbot_merge/hooks'), ['pull_request', 'issue_comment', 'status'])
    ])

def test_trivial_flow(env, repo):
    # create base branch
    m = repo.make_commit(None, "initial", None, tree={'a': 'some content'})
    repo.make_ref('heads/master', m)

    # create PR with 2 commits
    c0 = repo.make_commit(m, 'replace file contents', None, tree={'a': 'some other content'})
    c1 = repo.make_commit(c0, 'add file', None, tree={'a': 'some other content', 'b': 'a second file'})
    pr1 = repo.make_pr("gibberish", "blahblah", target='master', ctid=c1, user='user')

    [pr] = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', 'odoo/odoo'),
        ('number', '=', pr1.number),
    ])
    assert pr.state == 'opened'
    # nothing happened

    repo.post_status(c1, 'success', 'legal/cla')
    repo.post_status(c1, 'success', 'ci/runbot')
    assert pr.state == 'validated'

    pr1.post_comment('hansen r+', 'reviewer')
    assert pr.state == 'ready'

    env['runbot_merge.project']._check_progress()
    print(pr.read()[0])
    assert pr.staging_id

    # get head of staging branch
    staging_head = repo.commit('heads/staging.master')
    repo.post_status(staging_head.id, 'success', 'ci/runbot')
    repo.post_status(staging_head.id, 'success', 'legal/cla')

    env['runbot_merge.project']._check_progress()
    assert pr.state == 'merged'

    master = repo.commit('heads/master')
    assert master.parents == [m, pr1.head],\
        "master's parents should be the old master & the PR head"
    assert git.read_object(repo.objects, master.tree) == {
        'a': b'some other content',
        'b': b'a second file',
    }

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
        ('repository.name', '=', 'odoo/odoo'),
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
    pr2 = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', 'odoo/odoo'),
        ('number', '=', 2)
    ])
    assert pr2.state == 'ready', "PR2 should not have been staged since there is a pending staging for master"

    staging_head = repo.commit('heads/staging.master')
    repo.post_status(staging_head.id, 'success', 'ci/runbot')
    repo.post_status(staging_head.id, 'success', 'legal/cla')
    env['runbot_merge.project']._check_progress()
    assert pr1.state == 'merged'
    assert pr2.staging_id

    staging_head = repo.commit('heads/staging.master')
    repo.post_status(staging_head.id, 'success', 'ci/runbot')
    repo.post_status(staging_head.id, 'success', 'legal/cla')
    env['runbot_merge.project']._check_progress()
    assert pr2.state == 'merged'

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
        ('repository.name', '=', 'odoo/odoo'),
        ('number', '=', pr1.number)
    ])
    assert pr1.staging_id
    pr2 = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', 'odoo/odoo'),
        ('number', '=', pr2.number)
    ])
    assert pr2.staging_id

def test_staging_merge_fail(env, repo):
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
        ('repository.name', '=', 'odoo/odoo'),
        ('number', '=', prx.number)
    ])
    assert pr1.state == 'error'
    assert prx.comments == [
        ('reviewer', 'hansen r+'),
        ('<insert current user here>', 'Unable to stage PR (merge conflict)')
    ]

def test_staging_ci_timeout(env, repo):
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
        ('repository.name', '=', 'odoo/odoo'),
        ('number', '=', prx.number)
    ])
    assert pr1.staging_id
    timeout = env['runbot_merge.project'].search([]).ci_timeout

    pr1.staging_id.staged_at = odoo.fields.Datetime.to_string(datetime.datetime.now() - datetime.timedelta(minutes=2*timeout))
    env['runbot_merge.project']._check_progress()
    assert pr1.state == 'error', "%sth timeout should fail the PR" % (timeout + 1)

def test_staging_ci_failure_single(env, repo):
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
        ('repository.name', '=', 'odoo/odoo'),
        ('number', '=', prx.number)
    ]).staging_id

    staging_head = repo.commit('heads/staging.master')
    repo.post_status(staging_head.id, 'success', 'legal/cla')
    repo.post_status(staging_head.id, 'failure', 'ci/runbot') # stable genius
    env['runbot_merge.project']._check_progress()
    assert env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', 'odoo/odoo'),
        ('number', '=', prx.number)
    ]).state == 'error'

    assert prx.comments == [
        ('reviewer', 'hansen r+'),
        ('<insert current user here>', 'Staging failed: ci/runbot')
    ]

def test_ff_failure(env, repo):
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
        ('repository.name', '=', 'odoo/odoo'),
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
        ('repository.name', '=', 'odoo/odoo'),
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
        ('repository.name', '=', 'odoo/odoo'),
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

    prx.base = '1.0'
    assert env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', 'odoo/odoo'),
        ('number', '=', prx.number)
    ]).target == branch_1

def test_edit_retarget_managed(env, repo):
    """ A PR targeted to an un-managed branch is ignored but if the PR
    is re-targeted to a managed branch it should be managed

    TODO: maybe bot should tag PR as managed/unmanaged?
    """
@pytest.mark.skip(reason="What do?")
def test_edit_staged(env, repo):
    pass
@pytest.mark.skip(reason="What do?")
def test_close_staged(env, repo):
    pass

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
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).staging_id

        staging_head = repo.commit('heads/staging.master')
        repo.post_status(staging_head.id, 'success', 'legal/cla')
        repo.post_status(staging_head.id, 'failure', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
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
    def test_retry_comment(self, env, repo, retrier):
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
        prx.post_comment('hansen r+ delegate=other', "reviewer")
        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).staging_id

        staging_head = repo.commit('heads/staging.master')
        repo.post_status(staging_head.id, 'success', 'legal/cla')
        repo.post_status(staging_head.id, 'failure', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).state == 'error'

        prx.post_comment('hansen retry', retrier)
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).state == 'ready'
        env['runbot_merge.project']._check_progress()

        staging_head2 = repo.commit('heads/staging.master')
        assert staging_head2 != staging_head
        repo.post_status(staging_head2.id, 'success', 'legal/cla')
        repo.post_status(staging_head2.id, 'success', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).state == 'merged'

    @pytest.mark.parametrize('disabler', ['user', 'other', 'reviewer'])
    def test_retry_disable(self, env, repo, disabler):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        c2 = repo.make_commit(c1, 'second', None, tree={'m': 'c2'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')
        prx.post_comment('hansen r+ delegate=other', "reviewer")
        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).staging_id

        staging_head = repo.commit('heads/staging.master')
        repo.post_status(staging_head.id, 'success', 'legal/cla')
        repo.post_status(staging_head.id, 'failure', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
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

class TestSquashing(object):
    """
    * if event['pull_request']['commits'] == 1 and not disabled,
      squash-merge during staging (using sole commit's message) instead
      of regular merge (using PR info)
    * if 1+ commit but enabled, squash using PR info
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
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).squash

        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).staging_id

        staging = repo.commit('heads/staging.master')
        assert not git.is_ancestor(repo.objects, prx.head, of=staging.id),\
            "the pr head should not be an ancestor of the staging branch in a squash merge"
        assert staging.parents == [m2],\
            "the previous master's tip should be the sole parent of the staging commit"
        assert git.read_object(repo.objects, staging.tree) == {
            'm': b'c1', 'm2': b'm2',
        }, "the tree should still be correctly merged"

        repo.post_status(staging.id, 'success', 'legal/cla')
        repo.post_status(staging.id, 'success', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).state == 'merged'
        assert prx.state == 'closed'

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
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).squash

        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).staging_id

        staging = repo.commit('heads/staging.master')
        assert not git.is_ancestor(repo.objects, prx.head, of=staging.id),\
            "the pr head should not be an ancestor of the staging branch in a squash merge"
        assert staging.parents == [m2],\
            "the previous master's tip should be the sole parent of the staging commit"
        assert git.read_object(repo.objects, staging.tree) == {
            'm': b'c2', 'm2': b'm2',
        }, "the tree should still be correctly merged"

        repo.post_status(staging.id, 'success', 'legal/cla')
        repo.post_status(staging.id, 'success', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).state == 'merged'
        assert prx.state == 'closed'

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
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).squash

        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).staging_id

        staging = repo.commit('heads/staging.master')
        assert git.is_ancestor(repo.objects, prx.head, of=staging.id)
        assert staging.parents == [m2, c1]
        assert git.read_object(repo.objects, staging.tree) == {
            'm': b'c1', 'm2': b'm2',
        }

        repo.post_status(staging.id, 'success', 'legal/cla')
        repo.post_status(staging.id, 'success', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
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
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number),
        ])
        assert pr.head == c
        # alter & push force PR entirely
        c2 = repo.make_commit(m, 'first', None, tree={'m': 'cc'})
        prx.push(c2)
        assert pr.head == c2

    def test_update_closed(self, env, repo):
        """ Should warn that the PR is closed & update will be ignored
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c, user='user')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number),
        ])
        prx.close()
        assert pr.state == 'closed'
        assert pr.head == c

        c2 = repo.make_commit(c, 'first', None, tree={'m': 'cc'})
        prx.push(c2)
        assert pr.head == c2, "PR should still be updated in case it's reopened"
        assert prx.comments == [
            ('<insert current user here>', "This pull request is closed, ignoring the update to {}".format(c2)),
        ]

    def test_reopen_update(self, env, repo):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c, user='user')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
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
            ('repository.name', '=', 'odoo/odoo'),
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
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number),
        ])
        assert pr.head == c
        assert pr.state == 'approved'

        c2 = repo.make_commit(c, 'first', None, tree={'m': 'cc'})
        prx.push(c2)
        assert pr.head == c2
        assert pr.state == 'approved'

    def test_update_ready(self, env, repo):
        """ Should reset to approved
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+', user='reviewer')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number),
        ])
        assert pr.head == c
        assert pr.state == 'ready'

        c2 = repo.make_commit(c, 'first', None, tree={'m': 'cc'})
        prx.push(c2)
        assert pr.head == c2
        assert pr.state == 'approved'

    def test_update_staged(self, env, repo):
        """ Should cancel the staging & reset PR to approved
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+', user='reviewer')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number),
        ])
        env['runbot_merge.project']._check_progress()
        assert pr.state == 'ready'
        assert pr.staging_id

        c2 = repo.make_commit(c, 'first', None, tree={'m': 'cc'})
        prx.push(c2)
        assert pr.head == c2
        assert pr.state == 'approved'
        assert not pr.staging_id
        assert not env['runbot_merge.stagings'].search([])

    def test_update_merged(self, env, repo):
        """ Should warn that the PR is merged & ignore entirely
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+', user='reviewer')
        env['runbot_merge.project']._check_progress()

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number),
        ])
        h = repo.commit('heads/staging.master').id
        repo.post_status(h, 'success', 'legal/cla')
        repo.post_status(h, 'success', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        assert pr.state == 'merged'

        c2 = repo.make_commit(c, 'first', None, tree={'m': 'cc'})
        prx.push(c2)
        assert pr.head == c, "PR should not be updated at all"
        assert prx.comments == [
            ('reviewer', 'hansen r+'),
            ('<insert current user here>', 'Merged in {}'.format(h)),
            ('<insert current user here>', "This pull request is closed, ignoring the update to {}".format(c2)),
        ]
    def test_update_error(self, env, repo):
        """ Should cancel the staging & reset PR to approved
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+', user='reviewer')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
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
    def _pr(self, repo, prefix, trees, target='master', user='user'):
        """ Helper creating a PR from a series of commits on a base

        :param prefix: a prefix used for commit messages, PR title & PR body
        :param trees: a list of dicts symbolising the tree for the corresponding commit.
                      each tree is an update on the "current state" of the tree
        :param target: branch, both the base commit and the PR target
        """
        base = repo.commit('heads/{}'.format(target))
        tree = dict(repo.objects[base.tree])
        c = base.id
        for i, t in enumerate(trees):
            tree.update(t)
            c = repo.make_commit(c, 'commit_{}_{:02}'.format(prefix, i), None, tree=dict(tree))
        pr = repo.make_pr('title {}'.format(prefix), 'body {}'.format(prefix), target=target, ctid=c, user=user, label='{}:{}'.format(user, prefix))
        repo.post_status(c, 'success', 'ci/runbot')
        repo.post_status(c, 'success', 'legal/cla')
        pr.post_comment('hansen r+', 'reviewer')
        return pr

    def _get(self, env, number):
        return env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
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
        pr1 = self._get(env, pr1.number)
        assert pr1.staging_id
        pr2 = self._get(env, pr2.number)
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

        pr11 = self._pr(repo, 'Pressing 1', [{'x': 'x'}, {'y': 'y'}])
        pr12 = self._pr(repo, 'Pressing 2', [{'z': 'z'}, {'zz': 'zz'}])
        pr11.post_comment('hansen priority=1', 'reviewer')
        pr12.post_comment('hansen priority=1', 'reviewer')

        pr21, pr22, pr11, pr12 = prs = [self._get(env, pr.number) for pr in [pr21, pr22, pr11, pr12]]
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
        """ "Urgent" PRs should be selected before pressing & normal & batched together (?)

        TODO: should they also ignore validation aka immediately staged?
        """
        m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
        repo.make_ref('heads/master', m)

        pr21 = self._pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}])
        pr22 = self._pr(repo, 'PR2', [{'c': 'CCC'}, {'d': 'DDD'}])

        pr11 = self._pr(repo, 'Pressing 1', [{'x': 'x'}, {'y': 'y'}])
        pr12 = self._pr(repo, 'Pressing 2', [{'z': 'z'}, {'zz': 'zz'}])
        pr11.post_comment('hansen priority=1', 'reviewer')
        pr12.post_comment('hansen priority=1', 'reviewer')

        pr01 = self._pr(repo, 'Urgent 1', [{'n': 'n'}, {'o': 'o'}])
        pr02 = self._pr(repo, 'Urgent 2', [{'p': 'p'}, {'q': 'q'}])
        pr01.post_comment('hansen priority=0', 'reviewer')
        pr02.post_comment('hansen priority=0', 'reviewer')

        pr01, pr02, pr11, pr12, pr21, pr22 = prs = \
            [self._get(env, pr.number) for pr in [pr01, pr02, pr11, pr12, pr21, pr22]]
        assert pr01.priority == pr02.priority == 0
        assert pr11.priority == pr12.priority == 1
        assert pr21.priority == pr22.priority == 2

        env['runbot_merge.project']._check_progress()

        assert all(pr.state == 'ready' for pr in prs)
        assert pr01.staging_id
        assert pr02.staging_id
        assert pr01.staging_id == pr02.staging_id
        assert not pr11.staging_id
        assert not pr12.staging_id
        assert not pr21.staging_id
        assert not pr22.staging_id

    @pytest.mark.skip(reason="Maybe nothing to do, the PR is just skipped and put in error?")
    def test_batching_merge_failure(self, env, repo):
        pass

    def test_staging_ci_failure_batch(self, env, repo):
        """ on failure split batch & requeue
        """
        m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
        repo.make_ref('heads/master', m)

        c10 = repo.make_commit(m, 'AAA', None, tree={'a': 'AAA'})
        c11 = repo.make_commit(c10, 'BBB', None, tree={'a': 'AAA', 'b': 'BBB'})
        pr1 = repo.make_pr('t1', 'b1', target='master', ctid=c11, user='user', label='user:a')
        repo.post_status(pr1.head, 'success', 'ci/runbot')
        repo.post_status(pr1.head, 'success', 'legal/cla')
        pr1.post_comment('hansen r+', "reviewer")

        c20 = repo.make_commit(m, 'CCC', None, tree={'a': 'some content', 'c': 'CCC'})
        c21 = repo.make_commit(c20, 'DDD', None, tree={'a': 'some content', 'c': 'CCC', 'd': 'DDD'})
        pr2 = repo.make_pr('t2', 'b2', target='master', ctid=c21, user='user', label='user:b')
        repo.post_status(pr2.head, 'success', 'ci/runbot')
        repo.post_status(pr2.head, 'success', 'legal/cla')
        pr2.post_comment('hansen r+', "reviewer")

        env['runbot_merge.project']._check_progress()
        st = env['runbot_merge.stagings'].search([])
        # both prs should be part of the staging
        assert len(st.mapped('batch_ids.prs')) == 2
        # add CI failure
        h = repo.commit('heads/staging.master').id
        repo.post_status(h, 'failure', 'ci/runbot')
        repo.post_status(h, 'success', 'legal/cla')

        pr1 = env['runbot_merge.pull_requests'].search([('number', '=', pr1.number)])
        pr2 = env['runbot_merge.pull_requests'].search([('number', '=', pr2.number)])

        env['runbot_merge.project']._check_progress()
        # should have split the existing batch into two
        assert len(env['runbot_merge.stagings'].search([])) == 2
        assert pr1.staging_id and pr2.staging_id
        assert pr1.staging_id != pr2.staging_id
        assert pr1.staging_id.heads
        assert not pr2.staging_id.heads

        # This is the failing PR!
        h = repo.commit('heads/staging.master').id
        repo.post_status(h, 'failure', 'ci/runbot')
        repo.post_status(h, 'success', 'legal/cla')
        env['runbot_merge.project']._check_progress()
        assert pr1.state == 'error'
        assert pr2.staging_id.heads

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
        prx.post_comment('hansen r+', user='rando')

        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).state == 'validated'
        prx.post_comment('hansen r+', user='reviewer')
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).state == 'ready'

    def test_self_review_fail(self, env, repo):
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

        assert prx.user == 'reviewer'
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).state == 'validated'

    def test_self_review_success(self, env, repo):
        """ Some users are allowed to self-review
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
        repo.make_ref('heads/master', m2)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c1, user='self-reviewer')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+', user='self-reviewer')

        assert prx.user == 'self-reviewer'
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).state == 'ready'

    def test_delegate_review(self, env, repo):
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

        assert prx.user == 'user'
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).state == 'ready'

    def test_delegate_review_thirdparty(self, env, repo):
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
        prx.post_comment('hansen delegate=jimbob', user='reviewer')
        prx.post_comment('hansen r+', user='user')

        assert prx.user == 'user'
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).state == 'validated'

        prx.post_comment('hansen r+', user='jimbob')
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', 'odoo/odoo'),
            ('number', '=', prx.number)
        ]).state == 'ready'
