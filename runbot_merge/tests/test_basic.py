import datetime
import itertools
import json
import re
import time
from unittest import mock

import pytest
from lxml import html

import odoo

from test_utils import re_matches, run_crons

@pytest.fixture
def repo(make_repo):
    return make_repo('repo')

def test_trivial_flow(env, repo, page):
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
    env['runbot_merge.project']._send_feedback()
    assert pr1.labels == {'seen 🙂'}
    # nothing happened

    repo.post_status(c1, 'success', 'legal/cla')
    # rewrite status payload in old-style to ensure it does not break
    c = env['runbot_merge.commit'].search([('sha', '=', c1)])
    c.statuses = json.dumps({k: v['state'] for k, v in json.loads(c.statuses).items()})

    repo.post_status(c1, 'success', 'ci/runbot')
    assert pr.state == 'validated'
    env['runbot_merge.project']._check_progress()
    env['runbot_merge.project']._send_feedback()
    assert pr1.labels == {'seen 🙂', 'CI 🤖'}

    pr1.post_comment('hansen r+ rebase-merge', 'reviewer')
    assert pr.state == 'ready'

    # can't check labels here as running the cron will stage it

    env['runbot_merge.project']._check_progress()
    env['runbot_merge.project']._send_feedback()
    assert pr.staging_id
    assert pr1.labels == {'seen 🙂', 'CI 🤖', 'r+ 👌', 'merging 👷'}

    # get head of staging branch
    staging_head = repo.commit('heads/staging.master')
    repo.post_status(staging_head.id, 'success', 'ci/runbot', target_url='http://foo.com/pog')
    repo.post_status(staging_head.id, 'success', 'legal/cla')
    # the should not block the merge because it's not part of the requirements
    repo.post_status(staging_head.id, 'failure', 'ci/lint', target_url='http://ignored.com/whocares')

    assert set(tuple(t) for t in pr.staging_id.statuses) == {
        (repo.name, 'legal/cla', 'success', ''),
        (repo.name, 'ci/runbot', 'success', 'http://foo.com/pog'),
        (repo.name, 'ci/lint', 'failure', 'http://ignored.com/whocares'),
    }
    p = html.fromstring(page('/runbot_merge'))
    s = p.cssselect('.staging div.dropdown li')
    assert len(s) == 2
    assert s[0].get('class') == 'bg-success'
    assert s[0][0].text.strip() == '{}: ci/runbot'.format(repo.name)
    assert s[1].get('class') == 'bg-danger'
    assert s[1][0].text.strip() == '{}: ci/lint'.format(repo.name)

    assert re.match('^force rebuild', staging_head.message)

    env['runbot_merge.project']._check_progress()
    env['runbot_merge.project']._send_feedback()
    assert pr.state == 'merged'
    assert pr1.labels == {'seen 🙂', 'CI 🤖', 'r+ 👌', 'merged 🎉'}

    master = repo.commit('heads/master')
    # with default-rebase, only one parent is "known"
    assert master.parents[0] == m
    assert repo.read_tree(master) == {
        'a': b'some other content',
        'b': b'a second file',
    }
    assert master.message == "gibberish\n\nblahblah\n\ncloses {repo.name}#1"\
                             "\n\nSigned-off-by: Reviewer <reviewer@example.com>".format(repo=repo)

class TestCommitMessage:
    def test_commit_simple(self, env, repo, users):
        """ verify 'closes ...' is correctly added in the commit message
        """
        c1 = repo.make_commit(None, 'first!', None, tree={'f': 'm1'})
        repo.make_ref('heads/master', c1)
        c2 = repo.make_commit(c1, 'simple commit message', None, tree={'f': 'm2'})

        prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')
        prx.post_comment('hansen r+', "reviewer")

        env['runbot_merge.project']._check_progress()

        repo.post_status('heads/staging.master', 'success', 'ci/runbot')
        repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env['runbot_merge.project']._check_progress()

        master = repo.commit('heads/master')
        assert master.message == "simple commit message\n\ncloses {repo.name}#1"\
                                 "\n\nSigned-off-by: Reviewer <reviewer@example.com>".format(repo=repo)

    def test_commit_existing(self, env, repo, users):
        """ verify do not duplicate 'closes' instruction
        """
        c1 = repo.make_commit(None, 'first!', None, tree={'f': 'm1'})
        repo.make_ref('heads/master', c1)
        c2 = repo.make_commit(c1, 'simple commit message that closes #1', None, tree={'f': 'm2'})

        prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')
        prx.post_comment('hansen r+', "reviewer")

        env['runbot_merge.project']._check_progress()

        repo.post_status('heads/staging.master', 'success', 'ci/runbot')
        repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env['runbot_merge.project']._check_progress()

        master = repo.commit('heads/master')
        # closes #1 is already present, should not modify message
        assert master.message == "simple commit message that closes #1"\
                                 "\n\nSigned-off-by: Reviewer <reviewer@example.com>"

    def test_commit_other(self, env, repo, users):
        """ verify do not duplicate 'closes' instruction
        """
        c1 = repo.make_commit(None, 'first!', None, tree={'f': 'm1'})
        repo.make_ref('heads/master', c1)
        c2 = repo.make_commit(c1, 'simple commit message that closes odoo/enterprise#1', None, tree={'f': 'm2'})

        prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')
        prx.post_comment('hansen r+', "reviewer")

        env['runbot_merge.project']._check_progress()

        repo.post_status('heads/staging.master', 'success', 'ci/runbot')
        repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env['runbot_merge.project']._check_progress()

        master = repo.commit('heads/master')
        # closes on another repositoy, should modify the commit message
        assert master.message == "simple commit message that closes odoo/enterprise#1\n\ncloses {repo.name}#1"\
                                 "\n\nSigned-off-by: Reviewer <reviewer@example.com>".format(repo=repo)

    def test_commit_wrong_number(self, env, repo, users):
        """ verify do not match on a wrong number
        """
        c1 = repo.make_commit(None, 'first!', None, tree={'f': 'm1'})
        repo.make_ref('heads/master', c1)
        c2 = repo.make_commit(c1, 'simple commit message that closes #11', None, tree={'f': 'm2'})

        prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')
        prx.post_comment('hansen r+', "reviewer")

        env['runbot_merge.project']._check_progress()

        repo.post_status('heads/staging.master', 'success', 'ci/runbot')
        repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env['runbot_merge.project']._check_progress()

        master = repo.commit('heads/master')
        # closes on another repositoy, should modify the commit message
        assert master.message == "simple commit message that closes #11\n\ncloses {repo.name}#1"\
                                 "\n\nSigned-off-by: Reviewer <reviewer@example.com>".format(repo=repo)

    def test_commit_delegate(self, env, repo, users):
        """ verify 'signed-off-by ...' is correctly added in the commit message for delegated review
        """
        c1 = repo.make_commit(None, 'first!', None, tree={'f': 'm1'})
        repo.make_ref('heads/master', c1)
        c2 = repo.make_commit(c1, 'simple commit message', None, tree={'f': 'm2'})

        prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')
        prx.post_comment('hansen delegate=%s' % users['other'], "reviewer")
        prx.post_comment('hansen r+', user='other')

        env['runbot_merge.project']._check_progress()

        repo.post_status('heads/staging.master', 'success', 'ci/runbot')
        repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env['runbot_merge.project']._check_progress()

        master = repo.commit('heads/master')
        assert master.message == "simple commit message\n\ncloses {repo.name}#1"\
                                 "\n\nSigned-off-by: other <other@users.noreply.github.com>".format(repo=repo)


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
    pr1.post_comment("hansen r+ rebase-merge", "reviewer")
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
    pr2.post_comment('hansen r+ rebase-merge', "reviewer")
    env['runbot_merge.project']._check_progress()
    env['runbot_merge.project']._send_feedback()
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
    pr1.post_comment('hansen r+ rebase-merge', "reviewer")

    c20 = repo.make_commit(m, 'CCC', None, tree={'m': 'm', 'c': 'c'})
    c21 = repo.make_commit(c20, 'DDD', None, tree={'m': 'm', 'c': 'c', 'd': 'd'})
    pr2 = repo.make_pr('t2', 'b2', target='2.0', ctid=c21, user='user')
    repo.post_status(pr2.head, 'success', 'ci/runbot')
    repo.post_status(pr2.head, 'success', 'legal/cla')
    pr2.post_comment('hansen r+ rebase-merge', "reviewer")

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
    prx.post_comment('hansen r+ rebase-merge', "reviewer")

    env['runbot_merge.project']._check_progress()
    env['runbot_merge.project']._send_feedback()
    pr1 = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', prx.number)
    ])
    assert pr1.state == 'error'
    assert prx.labels == {'seen 🙂', 'error 🙅'}
    assert prx.comments == [
        (users['reviewer'], 'hansen r+ rebase-merge'),
        (users['user'], 'Merge method set to rebase and merge, using the PR as merge commit message'),
        (users['user'], re_matches('^Unable to stage PR')),
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
    prx.post_comment('hansen r+ rebase-merge', "reviewer")
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
    prx.post_comment('hansen r+ rebase-merge', "reviewer")
    env['runbot_merge.project']._check_progress()
    assert env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', prx.number)
    ]).staging_id

    staging_head = repo.commit('heads/staging.master')
    repo.post_status(staging_head.id, 'success', 'legal/cla')
    repo.post_status(staging_head.id, 'failure', 'ci/runbot') # stable genius
    run_crons(env)
    assert env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', prx.number)
    ]).state == 'error'

    assert prx.comments == [
        (users['reviewer'], 'hansen r+ rebase-merge'),
        (users['user'], "Merge method set to rebase and merge, using the PR as merge commit message"),
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
    prx.post_comment('hansen r+ rebase-merge', "reviewer")
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

def test_ff_failure_batch(env, repo, users):
    m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
    repo.make_ref('heads/master', m)

    a1 = repo.make_commit(m, 'a1', None, tree={'m': 'm', 'a': '1'})
    a2 = repo.make_commit(a1, 'a2', None, tree={'m': 'm', 'a': '2'})
    A = repo.make_pr('A', None, target='master', ctid=a2, user='user', label='A')
    repo.post_status(A.head, 'success', 'legal/cla')
    repo.post_status(A.head, 'success', 'ci/runbot')
    A.post_comment('hansen r+ rebase-merge', "reviewer")

    b1 = repo.make_commit(m, 'b1', None, tree={'m': 'm', 'b': '1'})
    b2 = repo.make_commit(b1, 'b2', None, tree={'m': 'm', 'b': '2'})
    B = repo.make_pr('B', None, target='master', ctid=b2, user='user', label='B')
    repo.post_status(B.head, 'success', 'legal/cla')
    repo.post_status(B.head, 'success', 'ci/runbot')
    B.post_comment('hansen r+ rebase-merge', "reviewer")

    c1 = repo.make_commit(m, 'c1', None, tree={'m': 'm', 'c': '1'})
    c2 = repo.make_commit(c1, 'c2', None, tree={'m': 'm', 'c': '2'})
    C = repo.make_pr('C', None, target='master', ctid=c2, user='user', label='C')
    repo.post_status(C.head, 'success', 'legal/cla')
    repo.post_status(C.head, 'success', 'ci/runbot')
    C.post_comment('hansen r+ rebase-merge', "reviewer")

    env['runbot_merge.project']._check_progress()
    messages = [
        c['commit']['message']
        for c in repo.log('heads/staging.master')
    ]
    assert 'a2' in messages
    assert 'b2' in messages
    assert 'c2' in messages

    # block FF
    m2 = repo.make_commit('heads/master', 'NO!', None, tree={'m': 'm2'})

    old_staging = repo.commit('heads/staging.master')
    # confirm staging
    repo.post_status('heads/staging.master', 'success', 'legal/cla')
    repo.post_status('heads/staging.master', 'success', 'ci/runbot')
    env['runbot_merge.project']._check_progress()
    new_staging = repo.commit('heads/staging.master')

    assert new_staging.id != old_staging.id

    # confirm again
    repo.post_status('heads/staging.master', 'success', 'legal/cla')
    repo.post_status('heads/staging.master', 'success', 'ci/runbot')
    env['runbot_merge.project']._check_progress()
    messages = {
        c['commit']['message']
        for c in repo.log('heads/master')
    }
    assert messages == {
        'initial', 'NO!',
        'a1', 'a2', 'A\n\ncloses {}#{}\n\nSigned-off-by: Reviewer <reviewer@example.com>'.format(repo.name, A.number),
        'b1', 'b2', 'B\n\ncloses {}#{}\n\nSigned-off-by: Reviewer <reviewer@example.com>'.format(repo.name, B.number),
        'c1', 'c2', 'C\n\ncloses {}#{}\n\nSigned-off-by: Reviewer <reviewer@example.com>'.format(repo.name, C.number),
    }

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
    env['runbot_merge.project']._send_feedback()
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
    env['runbot_merge.project']._send_feedback()

    assert not pr.staging_id
    assert not env['runbot_merge.stagings'].search([])
    assert pr.state == 'closed'
    assert prx.labels == {'seen 🙂', 'closed 💔'}

def test_forward_port(env, repo):
    m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
    repo.make_ref('heads/master', m)

    head = m
    for i in range(110):
        head = repo.make_commit(head, 'c_%03d' % i, None, tree={'m': 'm', 'f': str(i)}, wait=False)
    # for remote since we're not waiting in commit creation
    time.sleep(10)
    pr = repo.make_pr('PR', None, target='master', ctid=head, user='user')
    repo.post_status(pr.head, 'success', 'legal/cla')
    repo.post_status(pr.head, 'success', 'ci/runbot')
    pr.post_comment('hansen r+ merge', "reviewer")
    env['runbot_merge.project']._check_progress()

    st = repo.commit('heads/staging.master')
    assert st.message.startswith('force rebuild')

    repo.post_status(st.id, 'success', 'legal/cla')
    repo.post_status(st.id, 'success', 'ci/runbot')
    env['runbot_merge.project']._check_progress()

    h = repo.commit('heads/master')
    assert set(st.parents) == {h.id}
    assert set(h.parents) == {m, pr.head}
    commits = {c['sha'] for c in repo.log('heads/master')}
    assert len(commits) == 112

def test_rebase_failure(env, repo, users, remote_p):
    """ It looks like gh.rebase() can fail in the final ref-setting after
    the merging & commits creation has been performed. At this point, the
    staging will fail (yay) but the target branch (tmp) would not get reset,
    leading to the next PR being staged *on top* of the one being staged
    right there, and pretty much integrating it, leading to very, very
    strange results if the entire thing passes staging.

    Seen: https://github.com/odoo/odoo/pull/27835#issuecomment-430505429
    PR 27835 was merged to tmp at df0ae6c00e085dbaabcfec821208c9ace2f4b02d
    then the set_ref failed, following which PR 27840 is merged to tmp at
    819b5414c27a92031a9ce3f159a8f466a4fd698c note that the first (left)
    parent is the merge commit from PR 27835. The set_ref of PR 27840
    succeeded resulting in PR 27835 being integrated into the squashing of
    27840 (without any renaming or anything, just the content), following
    which PR 27835 was merged and squashed as a "no-content" commit.

    Problem: I need to make try_staging > stage > rebase > set_ref fail
    but only the first time, and not the set_ref in try_staging itself, and
    that call is performed *in a subprocess* when running <remote> tests.
    """
    # FIXME: remote mode
    if remote_p:
        pytest.skip("Needs to find a way to make set_ref fail on *second* call in remote mode.")

    m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
    repo.make_ref('heads/master', m)

    commit_a = repo.make_commit(m, 'A', None, tree={'m': 'm', 'a': 'a'})
    pr_a = repo.make_pr('A', None, target='master', ctid=commit_a, user='user', label='a')
    repo.post_status(pr_a.head, 'success', 'ci/runbot')
    repo.post_status(pr_a.head, 'success', 'legal/cla')
    pr_a.post_comment('hansen r+', 'reviewer')

    commit_b = repo.make_commit(m, 'B', None, tree={'m': 'm', 'b': 'b'})
    pr_b = repo.make_pr('B', None, target='master', ctid=commit_b, user='user', label='b')
    repo.post_status(pr_b.head, 'success', 'ci/runbot')
    repo.post_status(pr_b.head, 'success', 'legal/cla')
    pr_b.post_comment('hansen r+', 'reviewer')

    from odoo.addons.runbot_merge.github import GH
    original = GH.set_ref
    counter = itertools.count(start=1)
    def wrapper(*args):
        assert next(counter) != 2, "make it seem like updating the branch post-rebase fails"
        return original(*args)

    with mock.patch.object(GH, 'set_ref', autospec=True, side_effect=wrapper) as m:
        env['runbot_merge.project']._check_progress()

    env['runbot_merge.project']._send_feedback()

    assert pr_a.comments == [
        (users['reviewer'], 'hansen r+'),
        (users['user'], re_matches(r'^Unable to stage PR')),
    ]
    assert pr_b.comments == [
        (users['reviewer'], 'hansen r+'),
    ]
    assert repo.read_tree(repo.commit('heads/staging.master')) == {
        'm': b'm',
        'b': b'b',
    }

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
        prx.post_comment('hansen r+ delegate=%s rebase-merge' % users['other'], "reviewer")
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

    def test_retry_ignored(self, env, repo, users):
        """ Check feedback in case of ignored retry command on a non-error PR.
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        c2 = repo.make_commit(c1, 'second', None, tree={'m': 'c2'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
        prx.post_comment('hansen r+', 'reviewer')
        prx.post_comment('hansen retry', 'reviewer')

        env['runbot_merge.project']._send_feedback()
        assert prx.comments == [
            (users['reviewer'], 'hansen r+'),
            (users['reviewer'], 'hansen retry'),
            (users['user'], "I'm sorry, @{}. Retry makes no sense when the PR is not in error.".format(users['reviewer'])),
        ]

    @pytest.mark.parametrize('disabler', ['user', 'other', 'reviewer'])
    def test_retry_disable(self, env, repo, disabler, users):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        c2 = repo.make_commit(c1, 'second', None, tree={'m': 'c2'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c2, user='user')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')
        prx.post_comment('hansen r+ delegate=%s rebase-merge' % users['other'], "reviewer")
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
    def test_pr_single_commit(self, repo, env):
        """ If single commit, default to rebase & FF
        """
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
        assert re.match('^force rebuild', staging.message)
        assert repo.read_tree(staging) == {
            'm': b'c1', 'm2': b'm2',
        }, "the tree should still be correctly merged"
        [actual_sha] = staging.parents
        actual = repo.commit(actual_sha)
        assert actual.parents == [m2],\
            "dummy commit aside, the previous master's tip should be the sole parent of the staging commit"

        repo.post_status(staging.id, 'success', 'legal/cla')
        repo.post_status(staging.id, 'success', 'ci/runbot')
        env['runbot_merge.project']._check_progress()
        env['runbot_merge.project']._send_feedback()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'merged'
        assert prx.state == 'closed'

    def test_pr_update_to_many_commits(self, repo, env):
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

    def test_pr_reset_to_single_commit(self, repo, env):
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

    def test_pr_no_method(self, repo, env, users):
        """ a multi-repo PR should not be staged by default, should also get
        feedback indicating a merge method is necessary
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

        run_crons(env)
        assert not env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ]).staging_id

        assert prx.comments == [
            (users['reviewer'], 'hansen r+'),
            (users['user'], """Because this PR has multiple commits, I need to know how to merge it:

* `merge` to merge directly, using the PR as merge commit message
* `rebase-merge` to rebase and merge, using the PR as merge commit message
* `rebase-ff` to rebase and fast-forward
"""),
        ]

    def test_pr_method_no_review(self, repo, env, users):
        """ Configuring the method should be idependent from the review
        """
        m0 = repo.make_commit(None, 'M0', None, tree={'m': '0'})
        m1 = repo.make_commit(m0, 'M1', None, tree={'m': '1'})
        m2 = repo.make_commit(m1, 'M2', None, tree={'m': '2'})
        repo.make_ref('heads/master', m2)

        b0 = repo.make_commit(m1, 'B0', None, tree={'m': '1', 'b': '0'})
        b1 = repo.make_commit(b0, 'B1', None, tree={'m': '1', 'b': '1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=b1, user='user')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')

        prx.post_comment('hansen rebase-merge', "reviewer")
        assert pr.merge_method == 'rebase-merge'
        run_crons(env)

        prx.post_comment('hansen merge', "reviewer")
        assert pr.merge_method == 'merge'
        run_crons(env)

        prx.post_comment('hansen rebase-ff', "reviewer")
        assert pr.merge_method == 'rebase-ff'
        run_crons(env)

        assert prx.comments == [
            (users['reviewer'], 'hansen rebase-merge'),
            (users['user'], "Merge method set to rebase and merge, using the PR as merge commit message"),
            (users['reviewer'], 'hansen merge'),
            (users['user'], "Merge method set to merge directly, using the PR as merge commit message"),
            (users['reviewer'], 'hansen rebase-ff'),
            (users['user'], "Merge method set to rebase and fast-forward"),
        ]

    def test_pr_rebase_merge(self, repo, env):
        """ test result on rebase-merge

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

        # test commit ordering issue while at it: github sorts commits on
        # author.date instead of doing so topologically which is absolutely
        # not what we want
        committer = {'name': 'a', 'email': 'a', 'date': '2018-10-08T11:48:43Z'}
        author0 = {'name': 'a', 'email': 'a', 'date': '2018-10-01T14:58:38Z'}
        author1 = {'name': 'a', 'email': 'a', 'date': '2015-10-01T14:58:38Z'}
        b0 = repo.make_commit(m1, 'B0', author=author0, committer=committer, tree={'m': '1', 'b': '0'})
        b1 = repo.make_commit(b0, 'B1', author=author1, committer=committer, tree={'m': '1', 'b': '1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=b1, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+ rebase-merge', "reviewer")

        env['runbot_merge.project']._check_progress()

        # create a dag (msg:str, parents:set) from the log
        staging = log_to_node(repo.log('heads/staging.master'))
        # then compare to the dag version of the right graph
        nm2 = node('M2', node('M1', node('M0')))
        nb1 = node('B1', node('B0', nm2))
        merge_head = (
            'title\n\nbody\n\ncloses {}#{}\n\nSigned-off-by: Reviewer <reviewer@example.com>'.format(repo.name, prx.number),
            frozenset([nm2, nb1])
        )
        expected = (re_matches('^force rebuild'), frozenset([merge_head]))
        assert staging == expected

        repo.post_status('heads/staging.master', 'success', 'legal/cla')
        repo.post_status('heads/staging.master', 'success', 'ci/runbot')
        env['runbot_merge.project']._check_progress()

        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ]).state == 'merged'

        # check that the dummy commit is not in the final master
        master = log_to_node(repo.log('heads/master'))
        assert master == merge_head
        final_tree = repo.read_tree(repo.commit('heads/master'))
        assert final_tree == {'m': b'2', 'b': b'1'}, "sanity check of final tree"

    def test_pr_rebase_ff(self, repo, env):
        """ test result on rebase-merge

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
                |                          |
             +--+---+                   +--+---+
          PR |  B1  |                   |  B1  |
             +------+                   +--^---+
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
        prx.post_comment('hansen r+ rebase-ff', "reviewer")

        env['runbot_merge.project']._check_progress()

        # create a dag (msg:str, parents:set) from the log
        staging = log_to_node(repo.log('heads/staging.master'))
        # then compare to the dag version of the right graph
        nm2 = node('M2', node('M1', node('M0')))
        nb1 = node('B1\n\ncloses {}#{}\n\nSigned-off-by: Reviewer <reviewer@example.com>'.format(
            repo.name, prx.number), node('B0', nm2))
        expected = node(re_matches('^force rebuild'), nb1)
        assert staging == expected

        repo.post_status('heads/staging.master', 'success', 'legal/cla')
        repo.post_status('heads/staging.master', 'success', 'ci/runbot')
        env['runbot_merge.project']._check_progress()

        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ]).state == 'merged'

        # check that the dummy commit is not in the final master
        master = log_to_node(repo.log('heads/master'))
        assert master == nb1
        final_tree = repo.read_tree(repo.commit('heads/master'))
        assert final_tree == {'m': b'2', 'b': b'1'}, "sanity check of final tree"

    @pytest.mark.skip(reason="what do if the PR contains merge commits???")
    def test_pr_contains_merges(self, repo, env):
        pass

    def test_pr_force_merge_single_commit(self, repo, env):
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
        prx.post_comment('hansen r+ merge', 'reviewer')
        env['runbot_merge.project']._check_progress()

        repo.post_status('heads/staging.master', 'success', 'ci/runbot')
        repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env['runbot_merge.project']._check_progress()

        master = repo.commit('heads/master')
        assert master.parents == [m, prx.head], \
            "master's parents should be the old master & the PR head"

        m = node('M')
        c0 = node('C0', m)
        expected = node('gibberish\n\nblahblah\n\ncloses {}#{}'
                        '\n\nSigned-off-by: Reviewer <reviewer@example.com>'.format(repo.name, prx.number), m, c0)
        assert log_to_node(repo.log('heads/master')), expected

    def test_unrebase_emptymessage(self, repo, env):
        """ When merging between master branches (e.g. forward port), the PR
        may have only a title
        """
        m = repo.make_commit(None, "M", None, tree={'a': 'a'})
        repo.make_ref('heads/master', m)

        c0 = repo.make_commit(m, 'C0', None, tree={'a': 'b'})
        prx = repo.make_pr("gibberish", None, target='master', ctid=c0, user='user')
        env['runbot_merge.project']._check_progress()

        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+ merge', 'reviewer')
        env['runbot_merge.project']._check_progress()

        repo.post_status('heads/staging.master', 'success', 'ci/runbot')
        repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env['runbot_merge.project']._check_progress()

        master = repo.commit('heads/master')
        assert master.parents == [m, prx.head], \
            "master's parents should be the old master & the PR head"

        m = node('M')
        c0 = node('C0', m)
        expected = node('gibberish\n\ncloses {}#{}'
                        '\n\nSigned-off-by: Reviewer <reviewer@example.com>'.format(repo.name, prx.number), m, c0)
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
        prx.post_comment('hansen r+ merge', 'reviewer')
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
        prx.post_comment('hansen r+ merge', 'reviewer')
        env['runbot_merge.project']._check_progress()

        repo.post_status('heads/staging.master', 'success', 'ci/runbot')
        repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env['runbot_merge.project']._check_progress()

        master = repo.commit('heads/master')
        assert master.parents == [m2, c1]
        assert repo.read_tree(master) == {'a': b'1', 'b': b'2', 'bb': b'bb'}

        m1 = node('M1')
        expected = node(
            'T\n\nTT\n\ncloses {}#{}\n\nSigned-off-by: Reviewer <reviewer@example.com>'.format(repo.name, prx.number),
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
            pr.post_comment(
                'hansen r+%s' % (' rebase-merge' if len(trees) > 1 else ''),
                reviewer
            )
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

        log = list(repo.log('heads/staging.master'))
        staging = log_to_node(log)
        p1 = node(
            'title PR1\n\nbody PR1\n\ncloses {}#{}\n\nSigned-off-by: Reviewer <reviewer@example.com>'.format(repo.name, pr1.number),
            node('initial'),
            node('commit_PR1_01', node('commit_PR1_00', node('initial')))
        )
        p2 = node(
            'title PR2\n\nbody PR2\n\ncloses {}#{}\n\nSigned-off-by: Reviewer <reviewer@example.com>'.format(repo.name, pr2.number),
            p1,
            node('commit_PR2_01', node('commit_PR2_00', p1))
        )
        expected = (re_matches('^force rebuild'), frozenset([p2]))
        assert staging == expected

    def test_staging_batch_norebase(self, env, repo):
        """ If multiple PRs are ready for the same target at the same point,
        they should be staged together
        """
        m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
        repo.make_ref('heads/master', m)

        pr1 = self._pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}])
        pr1.post_comment('hansen merge', 'reviewer')
        pr2 = self._pr(repo, 'PR2', [{'c': 'CCC'}, {'d': 'DDD'}])
        pr2.post_comment('hansen merge', 'reviewer')

        env['runbot_merge.project']._check_progress()
        pr1 = self._get(env, repo, pr1.number)
        assert pr1.staging_id
        assert pr1.merge_method == 'merge'
        pr2 = self._get(env, repo, pr2.number)
        assert pr2.merge_method == 'merge'
        assert pr1.staging_id
        assert pr2.staging_id
        assert pr1.staging_id == pr2.staging_id

        log = list(repo.log('heads/staging.master'))

        staging = log_to_node(log)

        p1 = node(
            'title PR1\n\nbody PR1\n\ncloses {}#{}\n\nSigned-off-by: Reviewer <reviewer@example.com>'.format(repo.name, pr1.number),
            node('initial'),
            node('commit_PR1_01', node('commit_PR1_00', node('initial')))
        )
        p2 = node(
            'title PR2\n\nbody PR2\n\ncloses {}#{}\n\nSigned-off-by: Reviewer <reviewer@example.com>'.format(repo.name, pr2.number),
            p1,
            node('commit_PR2_01', node('commit_PR2_00', node('initial')))
        )
        expected = (re_matches('^force rebuild'), frozenset([p2]))
        assert staging == expected

    def test_staging_batch_squash(self, env, repo):
        """ If multiple PRs are ready for the same target at the same point,
        they should be staged together
        """
        m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
        repo.make_ref('heads/master', m)

        pr1 = self._pr(repo, 'PR1', [{'a': 'AAA'}])
        pr2 = self._pr(repo, 'PR2', [{'c': 'CCC'}])

        env['runbot_merge.project']._check_progress()
        pr1 = self._get(env, repo, pr1.number)
        assert pr1.staging_id
        pr2 = self._get(env, repo, pr2.number)
        assert pr1.staging_id
        assert pr2.staging_id
        assert pr1.staging_id == pr2.staging_id

        log = list(repo.log('heads/staging.master'))

        staging = log_to_node(log)
        expected = node(
            re_matches('^force rebuild'),
            node('commit_PR2_00\n\ncloses {}#{}\n\nSigned-off-by: Reviewer <reviewer@example.com>'.format(repo.name, pr2.number),
                 node('commit_PR1_00\n\ncloses {}#{}\n\nSigned-off-by: Reviewer <reviewer@example.com>'.format(repo.name, pr1.number),
                      node('initial'))))
        assert staging == expected

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
        pr01.post_comment('hansen priority=0 rebase-merge', 'reviewer')
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
    def test_reviewer_rights(self, env, repo, users):
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
        # second r+ to check warning
        prx.post_comment('hansen r+', user='reviewer')

        env['runbot_merge.project']._send_feedback()
        assert prx.comments == [
            (users['other'], 'hansen r+'),
            (users['reviewer'], 'hansen r+'),
            (users['reviewer'], 'hansen r+'),
            (users['user'], "I'm sorry, @{}. I'm afraid I can't do that.".format(users['other'])),
            (users['user'], "I'm sorry, @{}. This PR is already reviewed, reviewing it again is useless.".format(
                 users['reviewer'])),
        ]

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

        env['runbot_merge.project']._send_feedback()
        assert prx.comments == [
            (users['reviewer'], 'hansen r+'),
            (users['user'], "I'm sorry, @{}. You can't review+.".format(users['reviewer'])),
        ]

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

    def test_delegate_prefixes(self, env, repo):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'first', None, tree={'m': 'c'})
        prx = repo.make_pr('title', None, target='master', ctid=c, user='user')
        prx.post_comment('hansen delegate=foo,@bar,#baz', user='reviewer')

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])

        assert {d.github_login for d in pr.delegates} == {'foo', 'bar', 'baz'}


    def test_actual_review(self, env, repo):
        """ treat github reviews as regular comments
        """
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
        assert pr.state == 'opened'

        prx.post_review('REQUEST_CHANGES', 'reviewer', 'hansen priority=1')
        assert pr.priority == 1
        assert pr.state == 'opened'


        prx.post_review('COMMENT', 'reviewer', 'hansen r+')
        assert pr.priority == 1
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
        repo.post_status(prx.head, 'success', 'ci/runbot', target_url="http://example.org/wheee")

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

        c = env['runbot_merge.commit'].search([('sha', '=', prx.head)])
        assert json.loads(c.statuses) == {
            'legal/cla': {'state': 'success', 'target_url': None, 'description': None},
            'ci/runbot': {'state': 'success', 'target_url': 'http://example.org/wheee', 'description': None}
        }

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])
        assert pr.state == 'ready'

        env['runbot_merge.project']._check_progress()
        assert pr.staging_id

    def test_rplus_unmanaged(self, env, repo, users):
        """ r+ on an unmanaged target should notify about
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
        repo.make_ref('heads/branch', m2)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='branch', ctid=c1, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')

        prx.post_comment('hansen r+', "reviewer")

        env['runbot_merge.project']._check_fetch()
        env['runbot_merge.project']._send_feedback()

        assert prx.comments == [
            (users['reviewer'], 'hansen r+'),
            (users['user'], "I'm sorry. Branch `branch` is not within my remit."),
        ]

    def test_rplus_review_unmanaged(self, env, repo, users):
        """ r+ reviews can take a different path than comments
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
        repo.make_ref('heads/branch', m2)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='branch', ctid=c1, user='user')
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')

        prx.post_review('APPROVE', "reviewer", 'hansen r+')

        env['runbot_merge.project']._check_fetch()
        env['runbot_merge.project']._send_feedback()

        # FIXME: either split out reviews in local or merge reviews & comments in remote
        assert prx.comments[-1:] == [
            (users['user'], "I'm sorry. Branch `branch` is not within my remit."),
        ]

class TestRMinus:
    def test_rminus_approved(self, repo, env):
        """ approved -> r- -> opened
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'first', None, tree={'m': 'c'})
        prx = repo.make_pr('title', None, target='master', ctid=c, user='user')

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.state == 'opened'

        prx.post_comment('hansen r+', 'reviewer')
        assert pr.state == 'approved'

        prx.post_comment('hansen r-', 'user')
        assert pr.state == 'opened'
        prx.post_comment('hansen r+', 'reviewer')
        assert pr.state == 'approved'

        prx.post_comment('hansen r-', 'other')
        assert pr.state == 'approved'

        prx.post_comment('hansen r-', 'reviewer')
        assert pr.state == 'opened'

    def test_rminus_ready(self, repo, env):
        """ ready -> r- -> validated
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'first', None, tree={'m': 'c'})
        prx = repo.make_pr('title', None, target='master', ctid=c, user='user')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.state == 'validated'

        prx.post_comment('hansen r+', 'reviewer')
        assert pr.state == 'ready'

        prx.post_comment('hansen r-', 'user')
        assert pr.state == 'validated'
        prx.post_comment('hansen r+', 'reviewer')
        assert pr.state == 'ready'

        prx.post_comment('hansen r-', 'other')
        assert pr.state == 'ready'

        prx.post_comment('hansen r-', 'reviewer')
        assert pr.state == 'validated'

    def test_rminus_staged(self, repo, env):
        """ staged -> r- -> validated
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'first', None, tree={'m': 'c'})
        prx = repo.make_pr('title', None, target='master', ctid=c, user='user')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])

        # if reviewer unreviews, cancel staging & unreview
        prx.post_comment('hansen r+', 'reviewer')
        env['runbot_merge.project']._check_progress()
        st = pr.staging_id
        assert st

        prx.post_comment('hansen r-', 'reviewer')
        assert not st.active
        assert not pr.staging_id
        assert pr.state == 'validated'

        # if author unreviews, cancel staging & unreview
        prx.post_comment('hansen r+', 'reviewer')
        env['runbot_merge.project']._check_progress()
        st = pr.staging_id
        assert st

        prx.post_comment('hansen r-', 'user')
        assert not st.active
        assert not pr.staging_id
        assert pr.state == 'validated'

        # if rando unreviews, ignore
        prx.post_comment('hansen r+', 'reviewer')
        env['runbot_merge.project']._check_progress()
        st = pr.staging_id
        assert st

        prx.post_comment('hansen r-', 'other')
        assert pr.staging_id == st
        assert pr.state == 'ready'


class TestComments:
    def test_address_method(self, repo, env):
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c1, user='user')

        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen delegate=foo', user='reviewer')
        prx.post_comment('@hansen delegate=bar', user='reviewer')
        prx.post_comment('#hansen delegate=baz', user='reviewer')

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])

        assert {p.github_login for p in pr.delegates} \
            == {'foo', 'bar', 'baz'}

    def test_delete(self, repo, env):
        """ Comments being deleted should be ignored
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c1, user='user')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])

        cid = prx.post_comment('hansen r+', user='reviewer')
        # unreview by pushing a new commit
        prx.push(repo.make_commit(c1, 'second', None, tree={'m': 'c2'}))
        assert pr.state == 'opened'
        prx.delete_comment(cid, 'reviewer')
        # check that PR is still unreviewed
        assert pr.state == 'opened'

    def test_edit(self, repo, env):
        """ Comments being edited should be ignored
        """
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        prx = repo.make_pr('title', 'body', target='master', ctid=c1, user='user')
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])

        cid = prx.post_comment('hansen r+', user='reviewer')
        # unreview by pushing a new commit
        prx.push(repo.make_commit(c1, 'second', None, tree={'m': 'c2'}))
        assert pr.state == 'opened'
        prx.edit_comment(cid, 'hansen r+ edited', 'reviewer')
        # check that PR is still unreviewed
        assert pr.state == 'opened'

class TestInfrastructure:
    def test_protection(self, repo):
        """ force-pushing on a protected ref should fail
        """
        m0 = repo.make_commit(None, 'initial', None, tree={'m': 'm0'})
        m1 = repo.make_commit(m0, 'first', None, tree={'m': 'm1'})
        repo.make_ref('heads/master', m1)
        repo.protect('master')

        c1 = repo.make_commit(m0, 'other', None, tree={'m': 'c1'})
        with pytest.raises(AssertionError):
            repo.update_ref('heads/master', c1, force=True)
        assert repo.get_ref('heads/master') == m1

def node(name, *children):
    assert type(name) in (str, re_matches)
    return name, frozenset(children)
def log_to_node(log):
    log = list(log)
    nodes = {}
    # check that all parents are present
    ids = {c['sha'] for c in log}
    parents = {p['sha'] for c in log for p in c['parents']}
    missing = parents - ids
    assert parents, "Didn't find %s in log" % missing

    # github doesn't necessarily log topologically maybe?
    todo = list(reversed(log))
    while todo:
        c = todo.pop(0)
        if all(p['sha'] in nodes for p in c['parents']):
            nodes[c['sha']] = (c['commit']['message'], frozenset(
                nodes[p['sha']]
                for p in c['parents']
            ))
        else:
            todo.append(c)

    return nodes[log[0]['sha']]
