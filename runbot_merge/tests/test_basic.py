import datetime
import itertools
import json
import time
from unittest import mock

import pytest
import requests
from lxml import html

import odoo
from utils import _simple_init, seen, re_matches, get_partner, Commit, pr_page, to_pr, part_of


@pytest.fixture
def repo(env, project, make_repo, users, setreviewers):
    r = make_repo('repo')
    project.write({'repo_ids': [(0, 0, {
        'name': r.name,
        'group_id': False,
        'required_statuses': 'legal/cla,ci/runbot'
    })]})
    setreviewers(*project.repo_ids)
    return r

def test_trivial_flow(env, repo, page, users, config):
    # create base branch
    with repo:
        [m] = repo.make_commits(None, Commit("initial", tree={'a': 'some content'}), ref='heads/master')

        # create PR with 2 commits
        _, c1 = repo.make_commits(
            m,
            Commit('replace file contents', tree={'a': 'some other content'}),
            Commit('add file', tree={'b': 'a second file'}),
            ref='heads/other'
        )
        pr = repo.make_pr(title="gibberish", body="blahblah", target='master', head='other')

    pr_id = to_pr(env, pr)
    assert pr_id.state == 'opened'
    env.run_crons()
    assert pr.comments == [seen(env, pr, users)]

    pr_dashboard = pr_page(page, pr)
    s = pr_dashboard.cssselect('.alert-info > ul > li')
    assert [it.get('class') for it in s] == ['fail', 'fail', ''],\
        "merge method unset, review missing, no CI"
    assert dict(zip(
        [e.text_content() for e in pr_dashboard.cssselect('dl.runbot-merge-fields dt')],
        [e.text_content() for e in pr_dashboard.cssselect('dl.runbot-merge-fields dd')],
    )) == {
        'label': f"{config['github']['owner']}:other",
        'head': c1,
        'target': 'master',
    }

    with repo:
        repo.post_status(c1, 'success', 'legal/cla')
        repo.post_status(c1, 'success', 'ci/runbot')
    env.run_crons()
    assert pr_id.state == 'validated'

    s = pr_page(page, pr).cssselect('.alert-info > ul > li')
    assert [it.get('class') for it in s] == ['fail', 'fail', 'ok'],\
        "merge method unset, review missing, CI"
    statuses = [
        (l.find('a').text.split(':')[0], l.get('class').strip())
        for l in s[2].cssselect('ul li')
    ]
    assert statuses == [('legal/cla', 'ok'), ('ci/runbot', 'ok')]

    with repo:
        pr.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    assert pr_id.state == 'ready'

    # can't check labels here as running the cron will stage it

    env.run_crons()
    assert pr_id.staging_id
    assert pr_page(page, pr).cssselect('.alert-primary')

    with repo:
        # get head of staging branch
        staging_head = repo.commit('heads/staging.master')
        repo.post_status(staging_head.id, 'success', 'ci/runbot', target_url='http://foo.com/pog')
        repo.post_status(staging_head.id, 'success', 'legal/cla')
        # the should not block the merge because it's not part of the requirements
        repo.post_status(staging_head.id, 'failure', 'ci/lint', target_url='http://ignored.com/whocares')
    # need to store this because after the crons have run the staging will
    # have succeeded and been disabled
    st = pr_id.staging_id
    env.run_crons()

    assert {tuple(t) for t in st.statuses} == {
        (repo.name, 'legal/cla', 'success', ''),
        (repo.name, 'ci/runbot', 'success', 'http://foo.com/pog'),
        (repo.name, 'ci/lint', 'failure', 'http://ignored.com/whocares'),
    }

    p = html.fromstring(page('/runbot_merge'))
    s = p.cssselect('.staging div.dropdown a')
    assert len(s) == 2, "not logged so only *required* statuses"
    for e, status in zip(s, ['legal/cla', 'ci/runbot']):
        assert set(e.classes) == {'dropdown-item', 'bg-success'}
        assert e.text_content().strip() == f'{repo.name}: {status}'

    assert st.state == 'success'
    assert pr_id.state == 'merged'
    assert pr_page(page, pr).cssselect('.alert-success')

    master = repo.commit('heads/master')
    # with default-rebase, only one parent is "known"
    assert master.parents[0] == m
    assert repo.read_tree(master) == {
        'a': 'some other content',
        'b': 'a second file',
    }
    assert master.message == "gibberish\n\nblahblah\n\ncloses {repo.name}#1"\
                             "\n\nSigned-off-by: {reviewer.formatted_email}\n"\
                             .format(repo=repo, reviewer=get_partner(env, users['reviewer']))

class TestCommitMessage:
    def test_commit_simple(self, env, repo, users, config):
        """ verify 'closes ...' is correctly added in the commit message
        """
        with repo:
            c1 = repo.make_commit(None, 'first!', None, tree={'f': 'm1'})
            repo.make_ref('heads/master', c1)
            c2 = repo.make_commit(c1, 'simple commit message', None, tree={'f': 'm2'})

            prx = repo.make_pr(title='title', body='body', target='master', head=c2)
            repo.post_status(prx.head, 'success', 'ci/runbot')
            repo.post_status(prx.head, 'success', 'legal/cla')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('heads/staging.master', 'success', 'ci/runbot')
            repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env.run_crons()

        master = repo.commit('heads/master')
        assert master.message == "simple commit message\n\ncloses {repo.name}#1"\
                                 "\n\nSigned-off-by: {reviewer.formatted_email}\n"\
                                 .format(repo=repo, reviewer=get_partner(env, users['reviewer']))

    def test_commit_existing(self, env, repo, users, config):
        """ verify do not duplicate 'closes' instruction
        """
        with repo:
            c1 = repo.make_commit(None, 'first!', None, tree={'f': 'm1'})
            repo.make_ref('heads/master', c1)
            c2 = repo.make_commit(c1, 'simple commit message that closes #1', None, tree={'f': 'm2'})

            prx = repo.make_pr(title='title', body='body', target='master', head=c2)
            repo.post_status(prx.head, 'success', 'ci/runbot')
            repo.post_status(prx.head, 'success', 'legal/cla')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('heads/staging.master', 'success', 'ci/runbot')
            repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env.run_crons()

        master = repo.commit('heads/master')
        # closes #1 is already present, should not modify message
        assert master.message == "simple commit message that closes #1"\
                                 "\n\nSigned-off-by: {reviewer.formatted_email}\n"\
                                 .format(reviewer=get_partner(env, users['reviewer']))

    def test_commit_other(self, env, repo, users, config):
        """ verify do not duplicate 'closes' instruction
        """
        with repo:
            c1 = repo.make_commit(None, 'first!', None, tree={'f': 'm1'})
            repo.make_ref('heads/master', c1)
            c2 = repo.make_commit(c1, 'simple commit message that closes odoo/enterprise#1', None, tree={'f': 'm2'})

            prx = repo.make_pr(title='title', body='body', target='master', head=c2)
            repo.post_status(prx.head, 'success', 'ci/runbot')
            repo.post_status(prx.head, 'success', 'legal/cla')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('heads/staging.master', 'success', 'ci/runbot')
            repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env.run_crons()

        master = repo.commit('heads/master')
        # closes on another repositoy, should modify the commit message
        assert master.message == "simple commit message that closes odoo/enterprise#1\n\ncloses {repo.name}#1"\
                                 "\n\nSigned-off-by: {reviewer.formatted_email}\n"\
                                 .format(repo=repo, reviewer=get_partner(env, users['reviewer']))

    def test_commit_wrong_number(self, env, repo, users, config):
        """ verify do not match on a wrong number
        """
        with repo:
            c1 = repo.make_commit(None, 'first!', None, tree={'f': 'm1'})
            repo.make_ref('heads/master', c1)
            c2 = repo.make_commit(c1, 'simple commit message that closes #11', None, tree={'f': 'm2'})

            prx = repo.make_pr(title='title', body='body', target='master', head=c2)
            repo.post_status(prx.head, 'success', 'ci/runbot')
            repo.post_status(prx.head, 'success', 'legal/cla')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('heads/staging.master', 'success', 'ci/runbot')
            repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env.run_crons()

        master = repo.commit('heads/master')
        # closes on another repositoy, should modify the commit message
        assert master.message == "simple commit message that closes #11\n\ncloses {repo.name}#1"\
                                 "\n\nSigned-off-by: {reviewer.formatted_email}\n"\
                                 .format(repo=repo, reviewer=get_partner(env, users['reviewer']))

    def test_commit_delegate(self, env, repo, users, config):
        """ verify 'signed-off-by ...' is correctly added in the commit message for delegated review
        """
        env['res.partner'].create({
            'name': users['other'],
            'github_login': users['other'],
            'email': users['other'] + '@example.org'
        })
        with repo:
            c1 = repo.make_commit(None, 'first!', None, tree={'f': 'm1'})
            repo.make_ref('heads/master', c1)
            c2 = repo.make_commit(c1, 'simple commit message', None, tree={'f': 'm2'})

            prx = repo.make_pr(title='title', body='body', target='master', head=c2)
            repo.post_status(prx.head, 'success', 'ci/runbot')
            repo.post_status(prx.head, 'success', 'legal/cla')
            prx.post_comment('hansen delegate=%s' % users['other'], config["role_reviewer"]["token"])
            prx.post_comment('hansen r+', config['role_other']['token'])
        env.run_crons()

        with repo:
            repo.post_status('heads/staging.master', 'success', 'ci/runbot')
            repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env.run_crons()

        master = repo.commit('heads/master')
        assert master.message == "simple commit message\n\ncloses {repo.name}#1"\
                                 "\n\nSigned-off-by: {reviewer.formatted_email}\n"\
                                 .format(repo=repo, reviewer=get_partner(env, users['other']))

    def test_commit_coauthored(self, env, repo, users, config):
        """ verify 'closes ...' and 'Signed-off-by' are added before co-authored-by tags.

        Also checks that all co-authored-by are moved at the end of the
        message
        """
        with repo:
            c1 = repo.make_commit(None, 'first!', None, tree={'f': 'm1'})
            repo.make_ref('heads/master', c1)
            c2 = repo.make_commit(c1, '''simple commit message


Co-authored-by: Bob <bob@example.com>

Fixes a thing''', None, tree={'f': 'm2'})

            prx = repo.make_pr(title='title', body='body', target='master', head=c2)
            repo.post_status(prx.head, 'success', 'ci/runbot')
            repo.post_status(prx.head, 'success', 'legal/cla')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('heads/staging.master', 'success', 'ci/runbot')
            repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env.run_crons()

        master = repo.commit('heads/master')
        assert master.message == """simple commit message

Fixes a thing

closes {repo.name}#1

Signed-off-by: {reviewer.formatted_email}
Co-authored-by: Bob <bob@example.com>
""".format(
            repo=repo,
            reviewer=get_partner(env, users['reviewer'])
        )

class TestWebhookSecurity:
    def test_no_secret(self, env, project, repo):
        """ Test 1: didn't add a secret to the repo, should be ignored
        """
        project.secret = "a secret"

        with repo:
            m = repo.make_commit(None, "initial", None, tree={'a': 'some content'})
            repo.make_ref('heads/master', m)

            c0 = repo.make_commit(m, 'replace file contents', None, tree={'a': 'some other content'})
            pr0 = repo.make_pr(title="gibberish", body="blahblah", target='master', head=c0)

        assert not env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', pr0.number),
        ])

    def test_wrong_secret(self, env, project, repo):
        project.secret = "a secret"
        with repo:
            repo.set_secret("wrong secret")

            m = repo.make_commit(None, "initial", None, tree={'a': 'some content'})
            repo.make_ref('heads/master', m)

            c0 = repo.make_commit(m, 'replace file contents', None, tree={'a': 'some other content'})
            pr0 = repo.make_pr(title="gibberish", body="blahblah", target='master', head=c0)

        assert not env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', pr0.number),
        ])

    def test_correct_secret(self, env, project, repo):
        project.secret = "a secret"
        with repo:
            repo.set_secret("a secret")

            m = repo.make_commit(None, "initial", None, tree={'a': 'some content'})
            repo.make_ref('heads/master', m)

            c0 = repo.make_commit(m, 'replace file contents', None, tree={'a': 'some other content'})
            pr0 = repo.make_pr(title="gibberish", body="blahblah", target='master', head=c0)

        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', pr0.number),
        ])

def test_staging_ongoing(env, repo, config):
    with repo:
        # create base branch
        m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
        repo.make_ref('heads/master', m)

        # create PR
        c0 = repo.make_commit(m, 'replace file contents', None, tree={'a': 'some other content'})
        c1 = repo.make_commit(c0, 'add file', None, tree={'a': 'some other content', 'b': 'a second file'})
        pr1 = repo.make_pr(title="gibberish", body="blahblah", target='master', head=c1)
        repo.post_status(c1, 'success', 'legal/cla')
        repo.post_status(c1, 'success', 'ci/runbot')
        pr1.post_comment("hansen r+ rebase-merge", config['role_reviewer']['token'])
    env.run_crons()
    pr1 = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', 1)
    ])
    assert pr1.staging_id

    with repo:
        # create second PR and make ready for staging
        c2 = repo.make_commit(m, 'other', None, tree={'a': 'some content', 'c': 'ccc'})
        c3 = repo.make_commit(c2, 'other', None, tree={'a': 'some content', 'c': 'ccc', 'd': 'ddd'})
        pr2 = repo.make_pr(title='gibberish', body='blahblah', target='master', head=c3)
        repo.post_status(c3, 'success', 'legal/cla')
        repo.post_status(c3, 'success', 'ci/runbot')
        pr2.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()
    p_2 = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', pr2.number)
    ])
    assert p_2.state == 'ready', "PR2 should not have been staged since there is a pending staging for master"

    staging_head = repo.commit('heads/staging.master')
    with repo:
        repo.post_status(staging_head.id, 'success', 'ci/runbot')
        repo.post_status(staging_head.id, 'success', 'legal/cla')
    env.run_crons()
    assert pr1.state == 'merged'
    assert p_2.staging_id

    staging_head = repo.commit('heads/staging.master')
    with repo:
        repo.post_status(staging_head.id, 'success', 'ci/runbot')
        repo.post_status(staging_head.id, 'success', 'legal/cla')
    env.run_crons()
    assert p_2.state == 'merged'

def test_staging_concurrent(env, repo, config):
    """ test staging to different targets, should be picked up together """
    with repo:
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/1.0', m)
        repo.make_ref('heads/2.0', m)

    env['runbot_merge.project'].search([]).write({
        'branch_ids': [(0, 0, {'name': '1.0'}), (0, 0, {'name': '2.0'})],
    })

    with repo:
        c10 = repo.make_commit(m, 'AAA', None, tree={'m': 'm', 'a': 'a'})
        c11 = repo.make_commit(c10, 'BBB', None, tree={'m': 'm', 'a': 'a', 'b': 'b'})
        pr1 = repo.make_pr(title='t1', body='b1', target='1.0', head=c11)
        repo.post_status(pr1.head, 'success', 'ci/runbot')
        repo.post_status(pr1.head, 'success', 'legal/cla')
        pr1.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])

        c20 = repo.make_commit(m, 'CCC', None, tree={'m': 'm', 'c': 'c'})
        c21 = repo.make_commit(c20, 'DDD', None, tree={'m': 'm', 'c': 'c', 'd': 'd'})
        pr2 = repo.make_pr(title='t2', body='b2', target='2.0', head=c21)
        repo.post_status(pr2.head, 'success', 'ci/runbot')
        repo.post_status(pr2.head, 'success', 'legal/cla')
        pr2.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()

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

def test_staging_conflict_first(env, repo, users, config, page):
    """ If the first batch of a staging triggers a conflict, the PR should be
    marked as in error
    """
    with repo:
        m1 = repo.make_commit(None, 'initial', None, tree={'f': 'm1'})
        m2 = repo.make_commit(m1, 'second', None, tree={'f': 'm2'})
        repo.make_ref('heads/master', m2)

        c1 = repo.make_commit(m1, 'other second', None, tree={'f': 'c1'})
        c2 = repo.make_commit(c1, 'third', None, tree={'f': 'c2'})
        pr = repo.make_pr(title='title', body='body', target='master', head=c2)
        repo.post_status(pr.head, 'success', 'ci/runbot')
        repo.post_status(pr.head, 'success', 'legal/cla')
        pr.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()

    pr_id = to_pr(env, pr)
    assert pr_id.state == 'error'
    assert pr.comments == [
        (users['reviewer'], 'hansen r+ rebase-merge'),
        seen(env, pr, users),
        (users['user'], 'Merge method set to rebase and merge, using the PR as merge commit message.'),
        (users['user'], '@%(user)s @%(reviewer)s unable to stage: merge conflict' % users),
    ]

    dangerbox = pr_page(page, pr).cssselect('.alert-danger span')
    assert dangerbox
    assert dangerbox[0].text.strip() == 'Unable to stage PR'

def test_staging_conflict_second(env, repo, users, config):
    """ If the non-first batch of a staging triggers a conflict, the PR should
    just be skipped: it might be a conflict with an other PR which could fail
    the staging
    """
    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'a': '1'}), ref='heads/master')

    with repo:
        repo.make_commits(m, Commit('first pr', tree={'a': '2'}), ref='heads/pr0')
        pr0 = repo.make_pr(target='master', head='pr0')
        repo.post_status(pr0.head, 'success', 'ci/runbot')
        repo.post_status(pr0.head, 'success', 'legal/cla')
        pr0.post_comment('hansen r+', config['role_reviewer']['token'])

    with repo:
        repo.make_commits(m, Commit('second pr', tree={'a': '3'}), ref='heads/pr1')
        pr1 = repo.make_pr(target='master', head='pr1')
        repo.post_status(pr1.head, 'success', 'ci/runbot')
        repo.post_status(pr1.head, 'success', 'legal/cla')
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    pr0_id = to_pr(env, pr0)
    pr1_id = to_pr(env, pr1)
    assert pr0_id.staging_id, "pr0 should have been staged"
    assert not pr1_id.staging_id, "pr1 should not have been staged (due to conflict)"
    assert pr1_id.state == 'ready', "pr1 should not be in error yet"

    # merge the staging, this should try to stage pr1, fail, and put it in error
    # as it now conflicts with the master proper
    with repo:
        repo.post_status('staging.master', 'success', 'ci/runbot')
        repo.post_status('staging.master', 'success', 'legal/cla')
    env.run_crons()

    assert pr1_id.state == 'error', "now pr1 should be in error"


def test_staging_ci_timeout(env, repo, config, page):
    """If a staging timeouts (~ delay since staged greater than
    configured)... requeue?
    """
    with repo:
        m = repo.make_commit(None, 'initial', None, tree={'f': 'm'})
        repo.make_ref('heads/master', m)

        c1 = repo.make_commit(m, 'first', None, tree={'f': 'c1'})
        c2 = repo.make_commit(c1, 'second', None, tree={'f': 'c2'})
        pr = repo.make_pr(title='title', body='body', target='master', head=c2)
        repo.post_status(pr.head, 'success', 'ci/runbot')
        repo.post_status(pr.head, 'success', 'legal/cla')
        pr.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()

    pr_id = to_pr(env, pr)
    assert pr_id.staging_id
    timeout = env['runbot_merge.project'].search([]).ci_timeout

    pr_id.staging_id.staged_at = odoo.fields.Datetime.to_string(datetime.datetime.now() - datetime.timedelta(minutes=2*timeout))
    env.run_crons('runbot_merge.merge_cron', 'runbot_merge.staging_cron')
    assert pr_id.state == 'error', "timeout should fail the PR"

    dangerbox = pr_page(page, pr).cssselect('.alert-danger span')
    assert dangerbox
    assert dangerbox[0].text == 'timed out (>60 minutes)'

def test_timeout_bump_on_pending(env, repo, config):
    with repo:
        m = repo.make_commit(None, 'initial', None, tree={'f': '0'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'c', None, tree={'f': '1'})
        prx = repo.make_pr(title='title', body='body', target='master', head=c)
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')
        prx.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    st = env['runbot_merge.stagings'].search([])
    old_timeout = odoo.fields.Datetime.to_string(datetime.datetime.now() - datetime.timedelta(days=15))
    st.timeout_limit = old_timeout
    with repo:
        repo.post_status(repo.commit('heads/staging.master').id, 'pending', 'ci/runbot')
    env.run_crons('runbot_merge.process_updated_commits')
    assert st.timeout_limit > old_timeout

def test_staging_ci_failure_single(env, repo, users, config, page):
    """ on failure of single-PR staging, mark & notify failure
    """
    with repo:
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        c2 = repo.make_commit(c1, 'second', None, tree={'m': 'c2'})
        pr = repo.make_pr(title='title', body='body', target='master', head=c2)
        repo.post_status(pr.head, 'success', 'ci/runbot')
        repo.post_status(pr.head, 'success', 'legal/cla')
        pr.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()
    pr_id = to_pr(env, pr)
    assert pr_id.staging_id

    staging_head = repo.commit('heads/staging.master')
    with repo:
        repo.post_status(staging_head.id, 'failure', 'a/b')
        repo.post_status(staging_head.id, 'success', 'legal/cla')
        repo.post_status(staging_head.id, 'failure', 'ci/runbot') # stable genius
    env.run_crons()
    assert pr_id.state == 'error'

    assert pr.comments == [
        (users['reviewer'], 'hansen r+ rebase-merge'),
        seen(env, pr, users),
        (users['user'], "Merge method set to rebase and merge, using the PR as merge commit message."),
        (users['user'], '@%(user)s @%(reviewer)s staging failed: ci/runbot' % users)
    ]

    dangerbox = pr_page(page, pr).cssselect('.alert-danger span')
    assert dangerbox
    assert dangerbox[0].text == 'ci/runbot'

def test_ff_failure(env, repo, config, page):
    """ target updated while the PR is being staged => redo staging """
    with repo:
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
        c2 = repo.make_commit(c1, 'second', None, tree={'m': 'c2'})
        prx = repo.make_pr(title='title', body='body', target='master', head=c2)
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()
    st = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', prx.number)
    ]).staging_id
    assert st

    with repo:
        m2 = repo.make_commit('heads/master', 'cockblock', None, tree={'m': 'm', 'm2': 'm2'})
    assert repo.commit('heads/master').id == m2

    # report staging success & run cron to merge
    staging = repo.commit('heads/staging.master')
    with repo:
        repo.post_status(staging.id, 'success', 'legal/cla')
        repo.post_status(staging.id, 'success', 'ci/runbot')
    env.run_crons()

    assert st.reason == 'update is not a fast forward'
    # check that it's added as title on the staging
    doc = html.fromstring(page('/runbot_merge'))
    _new, prev = doc.cssselect('li.staging')

    assert 'bg-gray-lighter' in prev.classes, "ff failure is ~ cancelling"
    assert prev.get('title') == re_matches('fast forward failed \(update is not a fast forward\)')

    assert env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', prx.number)
    ]).staging_id, "merge should not have succeeded"
    assert repo.commit('heads/staging.master').id != staging.id,\
        "PR should be staged to a new commit"

def test_ff_failure_batch(env, repo, users, config):
    with repo:
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        a1 = repo.make_commit(m, 'a1', None, tree={'m': 'm', 'a': '1'})
        a2 = repo.make_commit(a1, 'a2', None, tree={'m': 'm', 'a': '2'})
        repo.make_ref('heads/A', a2)
        A = repo.make_pr(title='A', body=None, target='master', head='A')
        repo.post_status(A.head, 'success', 'legal/cla')
        repo.post_status(A.head, 'success', 'ci/runbot')
        A.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])

        b1 = repo.make_commit(m, 'b1', None, tree={'m': 'm', 'b': '1'})
        b2 = repo.make_commit(b1, 'b2', None, tree={'m': 'm', 'b': '2'})
        repo.make_ref('heads/B', b2)
        B = repo.make_pr(title='B', body=None, target='master', head='B')
        repo.post_status(B.head, 'success', 'legal/cla')
        repo.post_status(B.head, 'success', 'ci/runbot')
        B.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])

        c1 = repo.make_commit(m, 'c1', None, tree={'m': 'm', 'c': '1'})
        c2 = repo.make_commit(c1, 'c2', None, tree={'m': 'm', 'c': '2'})
        repo.make_ref('heads/C', c2)
        C = repo.make_pr(title='C', body=None, target='master', head='C')
        repo.post_status(C.head, 'success', 'legal/cla')
        repo.post_status(C.head, 'success', 'ci/runbot')
        C.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()

    pr_a = to_pr(env, A)
    pr_b = to_pr(env, B)
    pr_c = to_pr(env, C)

    messages = [
        c['commit']['message']
        for c in repo.log('heads/staging.master')
    ]
    assert part_of('a2', pr_a) in messages
    assert part_of('b2', pr_b) in messages
    assert part_of('c2', pr_c) in messages

    # block FF
    with repo:
        repo.make_commit('heads/master', 'NO!', None, tree={'m': 'm2'})

    old_staging = repo.commit('heads/staging.master')
    # confirm staging
    with repo:
        repo.post_status('heads/staging.master', 'success', 'legal/cla')
        repo.post_status('heads/staging.master', 'success', 'ci/runbot')
    env.run_crons()
    new_staging = repo.commit('heads/staging.master')

    assert new_staging.id != old_staging.id

    # confirm again
    with repo:
        repo.post_status('heads/staging.master', 'success', 'legal/cla')
        repo.post_status('heads/staging.master', 'success', 'ci/runbot')
    env.run_crons()
    messages = {
        c['commit']['message']
        for c in repo.log('heads/master')
    }
    reviewer = get_partner(env, users["reviewer"]).formatted_email
    assert messages == {
        'initial', 'NO!',
        part_of('a1', pr_a), part_of('a2', pr_a), f'A\n\ncloses {pr_a.display_name}\n\nSigned-off-by: {reviewer}\n',
        part_of('b1', pr_b), part_of('b2', pr_b), f'B\n\ncloses {pr_b.display_name}\n\nSigned-off-by: {reviewer}\n',
        part_of('c1', pr_c), part_of('c2', pr_c), f'C\n\ncloses {pr_c.display_name}\n\nSigned-off-by: {reviewer}\n',
    }

class TestPREdition:
    def test_edit(self, env, repo, config):
        """ Editing PR:

        * title (-> message)
        * body (-> message)
        * base.ref (-> target)
        """
        branch_1 = env['runbot_merge.branch'].create({
            'name': '1.0',
            'project_id': env['runbot_merge.project'].search([]).id,
        })

        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)
            repo.make_ref('heads/1.0', m)
            repo.make_ref('heads/2.0', m)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            c2 = repo.make_commit(c1, 'second', None, tree={'m': 'c2'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c2)
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen rebase-ff r+', config['role_reviewer']['token'])
        env.run_crons()

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.state == 'ready'
        st = pr.staging_id
        assert st
        assert pr.message == 'title\n\nbody'
        with repo: prx.title = "title 2"
        assert pr.message == 'title 2\n\nbody'
        with repo: prx.body = None
        assert pr.message == "title 2"
        assert pr.staging_id, \
            "message edition does not affect staging of rebased PRs"
        with repo: prx.base = '1.0'
        assert pr.target == branch_1
        assert not pr.staging_id, "updated the base of a staged PR should have unstaged it"
        assert st.reason == f"{pr.display_name} target (base) branch was changed from 'master' to '1.0'"

        with repo: prx.base = '2.0'
        assert not pr.exists()
        env.run_crons()

        with repo: prx.base = '1.0'
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).target == branch_1

    def test_retarget_update_commits(self, env, repo):
        """ Retargeting a PR should update its commits count
        """
        branch_1 = env['runbot_merge.branch'].create({
            'name': '1.0',
            'project_id': env['runbot_merge.project'].search([]).id,
        })
        master = env['runbot_merge.branch'].search([('name', '=', 'master')])

        with repo:
            # master is 1 commit ahead of 1.0
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/1.0', m)
            m2 = repo.make_commit(m, 'second', None, tree={'m': 'm2'})
            repo.make_ref('heads/master', m2)

            # the PR builds on master, but is errorneously targeted to 1.0
            c = repo.make_commit(m2, 'first', None, tree={'m': 'm3'})
            prx = repo.make_pr(title='title', body='body', target='1.0', head=c)
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])
        assert not pr.squash

        with repo:
            prx.base = 'master'
        assert pr.target == master
        assert pr.squash

        with repo:
            prx.base = '1.0'
        assert pr.target == branch_1
        assert not pr.squash

        # check if things also work right when modifying the PR then
        # retargeting (don't see why not but...)
        with repo:
            c2 = repo.make_commit(m2, 'xxx', None, tree={'m': 'm4'})
            repo.update_ref(prx.ref, c2, force=True)
        assert pr.head == c2
        assert not pr.squash
        with repo:
            prx.base = 'master'
        assert pr.squash

    @pytest.mark.xfail(reason="github doesn't allow retargeting closed PRs", strict=True)
    def test_retarget_closed(self, env, repo):
        branch_1 = env['runbot_merge.branch'].create({
            'name': '1.0',
            'project_id': env['runbot_merge.project'].search([]).id,
        })

        with repo:
            # master is 1 commit ahead of 1.0
            [m] = repo.make_commits(None, repo.Commit('initial', tree={'1': '1'}), ref='heads/1.0')
            repo.make_commits(m, repo.Commit('second', tree={'m': 'm'}), ref='heads/master')

            [c] = repo.make_commits(m, repo.Commit('first', tree={'m': 'm3'}), ref='heads/abranch')
            prx = repo.make_pr(title='title', body='body', target='1.0', head=c)
        env.run_crons()
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])
        assert pr.target == branch_1
        with repo:
            prx.close()
        with repo:
            prx.base = 'master'

def test_close_staged(env, repo, config, page):
    """
    When closing a staged PR, cancel the staging
    """
    with repo:
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
        prx = repo.make_pr(title='title', body='body', target='master', head=c)
        repo.post_status(prx.head, 'success', 'legal/cla')
        repo.post_status(prx.head, 'success', 'ci/runbot')
        prx.post_comment('hansen r+', config['role_reviewer']['token'])
    pr = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', prx.number),
    ])
    env.run_crons()
    assert pr.state == 'ready'
    assert pr.staging_id

    with repo:
        prx.close()
    env.run_crons()

    assert not pr.staging_id
    assert not env['runbot_merge.stagings'].search([])
    assert pr.state == 'closed'
    assert pr_page(page, prx).cssselect('.alert-light')

def test_forward_port(env, repo, config):
    with repo:
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        head = m
        for i in range(110):
            head = repo.make_commit(head, 'c_%03d' % i, None, tree={'m': 'm', 'f': str(i)})
    # not sure why we wanted to wait here

    with repo:
        pr = repo.make_pr(title='PR', body=None, target='master', head=head)
        repo.post_status(pr.head, 'success', 'legal/cla')
        repo.post_status(pr.head, 'success', 'ci/runbot')
        pr.post_comment('hansen r+ merge', config['role_reviewer']['token'])
    env.run_crons()

    st = repo.commit('staging.master')

    with repo:
        repo.post_status(st.id, 'success', 'legal/cla')
        repo.post_status(st.id, 'success', 'ci/runbot')
    env.run_crons()

    h = repo.commit('master')
    assert st.id == h.id
    assert set(h.parents) == {m, pr.head}
    commits = {c['sha'] for c in repo.log('master')}
    assert len(commits) == 112

@pytest.mark.skip("Needs to find a way to make set_ref fail on *second* call.")
def test_rebase_failure(env, repo, users, config):
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
    with repo:
        m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
        repo.make_ref('heads/master', m)

        commit_a = repo.make_commit(m, 'A', None, tree={'m': 'm', 'a': 'a'})
        repo.make_ref('heads/a', commit_a)
        pr_a = repo.make_pr(title='A', body=None, target='master', head='a')
        repo.post_status(pr_a.head, 'success', 'ci/runbot')
        repo.post_status(pr_a.head, 'success', 'legal/cla')
        pr_a.post_comment('hansen r+', config['role_reviewer']['token'])

        commit_b = repo.make_commit(m, 'B', None, tree={'m': 'm', 'b': 'b'})
        repo.make_ref('heads/b', commit_b)
        pr_b = repo.make_pr(title='B', body=None, target='master', head='b')
        repo.post_status(pr_b.head, 'success', 'ci/runbot')
        repo.post_status(pr_b.head, 'success', 'legal/cla')
        pr_b.post_comment('hansen r+', config['role_reviewer']['token'])

    from odoo.addons.runbot_merge.github import GH
    original = GH.set_ref
    counter = itertools.count(start=1)
    def wrapper(*args):
        assert next(counter) != 2, "make it seem like updating the branch post-rebase fails"
        return original(*args)

    env['runbot_merge.commit']._notify()
    with mock.patch.object(GH, 'set_ref', autospec=True, side_effect=wrapper):
        env['runbot_merge.project']._check_progress()

    env['runbot_merge.pull_requests.feedback']._send()

    assert pr_a.comments == [
        (users['reviewer'], 'hansen r+'),
        seen(env, pr_a, users),
        (users['user'], re_matches(r'^Unable to stage PR')),
    ]
    assert pr_b.comments == [
        (users['reviewer'], 'hansen r+'),
        seen(env, pr_b, users),
    ]
    assert repo.read_tree(repo.commit('heads/staging.master')) == {
        'm': 'm',
        'b': 'b',
    }

def test_ci_failure_after_review(env, repo, users, config):
    """ If a PR is r+'d but the CI ends up failing afterwards, ping the user
    so they're aware. This is useful for the more "fire and forget" approach
    especially small / simple PRs where you assume they're going to pass and
    just r+ immediately.
    """
    with repo:
        prx = _simple_init(repo)
        prx.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    for ctx, url in [
        ('ci/runbot', 'https://a'),
        ('ci/runbot', 'https://a'),
        ('legal/cla', 'https://b'),
        ('foo/bar', 'https://c'),
        ('ci/runbot', 'https://a'),
        ('legal/cla', 'https://d'), # url changes so different from the previous
    ]:
        with repo:
            repo.post_status(prx.head, 'failure', ctx, target_url=url)
        env.run_crons()

    assert prx.comments == [
        (users['reviewer'], 'hansen r+'),
        seen(env, prx, users),
        (users['user'], "@{user} @{reviewer} 'ci/runbot' failed on this reviewed PR.".format_map(users)),
        (users['user'], "@{user} @{reviewer} 'legal/cla' failed on this reviewed PR.".format_map(users)),
        (users['user'], "@{user} @{reviewer} 'legal/cla' failed on this reviewed PR.".format_map(users)),
    ]

def test_reopen_merged_pr(env, repo, config, users):
    """ Reopening a *merged* PR should cause us to immediately close it again,
    and insult whoever did it
    """
    with repo:
        [m] = repo.make_commits(
            None,
            repo.Commit('initial', tree={'0': '0'}),
            ref = 'heads/master'
        )

        [c] = repo.make_commits(
            m, repo.Commit('second', tree={'0': '1'}),
            ref='heads/abranch'
        )
        prx = repo.make_pr(target='master', head='abranch')
        repo.post_status(c, 'success', 'legal/cla')
        repo.post_status(c, 'success', 'ci/runbot')
        prx.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with repo:
        repo.post_status('staging.master', 'success', 'legal/cla')
        repo.post_status('staging.master', 'success', 'ci/runbot')
    env.run_crons()
    pr = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', prx.number)
    ])
    assert prx.state == 'closed'
    assert pr.state == 'merged'

    repo.add_collaborator(users['other'], config['role_other']['token'])
    with repo:
        prx.open(config['role_other']['token'])
    env.run_crons()
    assert prx.state == 'closed'
    assert pr.state == 'merged'
    assert prx.comments == [
        (users['reviewer'], 'hansen r+'),
        seen(env, prx, users),
        (users['user'], "@%s ya silly goose you can't reopen a merged PR." % users['other'])
    ]

class TestNoRequiredStatus:
    def test_basic(self, env, repo, config):
        """ check that mergebot can work on a repo with no CI at all
        """
        env['runbot_merge.repository'].search([('name', '=', repo.name)]).status_ids = False
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'0': '0'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'first', None, tree={'0': '1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c)
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])
        assert pr.state == 'ready'
        st = pr.staging_id
        assert st
        env.run_crons()
        assert st.state == 'success'
        assert pr.state == 'merged'

    def test_updated(self, env, repo, config):
        env['runbot_merge.repository'].search([('name', '=', repo.name)]).status_ids = False
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'0': '0'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'first', None, tree={'0': '1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c)
        env.run_crons()

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])
        assert pr.state == 'validated'

        # normal push
        with repo:
            repo.make_commits(c, repo.Commit('second', tree={'0': '2'}), ref=prx.ref)
        env.run_crons()
        assert pr.state == 'validated'
        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        assert pr.state == 'ready'

        # force push
        with repo:
            repo.make_commits(m, repo.Commit('xxx', tree={'0': 'm'}), ref=prx.ref)
        env.run_crons()
        assert pr.state == 'validated'
        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        assert pr.state == 'ready'

class TestRetry:
    @pytest.mark.xfail(reason="This may not be a good idea as it could lead to tons of rebuild spam")
    def test_auto_retry_push(self, env, repo, config):
        prx = _simple_init(repo)
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')
        prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).staging_id

        staging_head = repo.commit('heads/staging.master')
        repo.post_status(staging_head.id, 'success', 'legal/cla')
        repo.post_status(staging_head.id, 'failure', 'ci/runbot')
        env.run_crons()
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])
        assert pr.state == 'error'

        repo.update_ref(prx.ref, repo.make_commit(prx.head, 'third', None, tree={'m': 'c3'}), force=True)
        assert pr.state == 'approved'
        env['runbot_merge.project']._check_progress()
        assert pr.state == 'approved'
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')
        env.run_crons()
        assert pr.state == 'ready'

        staging_head2 = repo.commit('heads/staging.master')
        assert staging_head2 != staging_head
        repo.post_status(staging_head2.id, 'success', 'legal/cla')
        repo.post_status(staging_head2.id, 'success', 'ci/runbot')
        env.run_crons()
        assert pr.state == 'merged'

    @pytest.mark.parametrize('retrier', ['user', 'other', 'reviewer'])
    def test_retry_comment(self, env, repo, retrier, users, config):
        """ An accepted but failed PR should be re-tried when the author or a
        reviewer asks for it
        """
        with repo:
            prx = _simple_init(repo)
            repo.post_status(prx.head, 'success', 'ci/runbot')
            repo.post_status(prx.head, 'success', 'legal/cla')
            prx.post_comment('hansen r+ delegate=%s rebase-merge' % users['other'],
                             config["role_reviewer"]['token'])
        env.run_crons()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).staging_id

        staging_head = repo.commit('heads/staging.master')
        with repo:
            repo.post_status(staging_head.id, 'success', 'legal/cla')
            repo.post_status(staging_head.id, 'failure', 'ci/runbot')
        env.run_crons()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'error'

        with repo:
            prx.post_comment('hansen retry', config['role_' + retrier]['token'])
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'ready'
        env.run_crons('runbot_merge.merge_cron', 'runbot_merge.staging_cron')

        staging_head2 = repo.commit('heads/staging.master')
        assert staging_head2 != staging_head
        with repo:
            repo.post_status(staging_head2.id, 'success', 'legal/cla')
            repo.post_status(staging_head2.id, 'success', 'ci/runbot')
        env.run_crons()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'merged'

    def test_retry_again_message(self, env, repo, users, config, page):
        """ For a retried PR, the error message on the PR's page should be the
        later staging
        """
        with repo:
            pr = _simple_init(repo)
            repo.post_status(pr.head, 'success', 'ci/runbot')
            repo.post_status(pr.head, 'success', 'legal/cla')
            pr.post_comment('hansen r+ delegate=%s rebase-merge' % users['other'],
                             config["role_reviewer"]['token'])
        env.run_crons()
        pr_id = to_pr(env, pr)
        assert pr_id.staging_id

        with repo:
            repo.post_status('staging.master', 'success', 'legal/cla')
            repo.post_status('staging.master', 'failure', 'ci/runbot',
                             target_url='https://example.com/whocares')
        env.run_crons()
        assert pr_id.state == 'error'

        with repo:
            pr.post_comment('hansen retry', config['role_reviewer']['token'])
        env.run_crons('runbot_merge.merge_cron', 'runbot_merge.staging_cron')

        with repo:
            repo.post_status('staging.master', 'success', 'legal/cla')
            repo.post_status('staging.master', 'failure', 'ci/runbot',
                             target_url='https://example.com/ohno')
        env.run_crons()
        assert pr_id.state == 'error'

        dangerbox = pr_page(page, pr).cssselect('.alert-danger span')
        assert dangerbox
        assert dangerbox[0].text == 'ci/runbot (view more at https://example.com/ohno)'

    def test_retry_ignored(self, env, repo, users, config):
        """ Check feedback in case of ignored retry command on a non-error PR.
        """
        with repo:
            prx = _simple_init(repo)
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
            prx.post_comment('hansen retry', config['role_reviewer']['token'])
        env.run_crons()

        assert prx.comments == [
            (users['reviewer'], 'hansen r+'),
            (users['reviewer'], 'hansen retry'),
            seen(env, prx, users),
            (users['user'], "I'm sorry, @{reviewer}: retry makes no sense when the PR is not in error.".format_map(users)),
        ]

    @pytest.mark.parametrize('disabler', ['user', 'other', 'reviewer'])
    def test_retry_disable(self, env, repo, disabler, users, config):
        with repo:
            prx = _simple_init(repo)
            repo.post_status(prx.head, 'success', 'ci/runbot')
            repo.post_status(prx.head, 'success', 'legal/cla')
            prx.post_comment('hansen r+ delegate=%s rebase-merge' % users['other'],
                             config["role_reviewer"]['token'])
        env.run_crons()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).staging_id

        staging_head = repo.commit('heads/staging.master')
        with repo:
            repo.post_status(staging_head.id, 'success', 'legal/cla')
            repo.post_status(staging_head.id, 'failure', 'ci/runbot')
        env.run_crons()
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])
        assert pr.state == 'error'

        with repo:
            prx.post_comment('hansen r-', config['role_' + disabler]['token'])
        assert pr.state == 'validated'
        with repo:
            repo.make_commit(prx.ref, 'third', None, tree={'m': 'c3'})
            # just in case, apparently in some case the first post_status uses the old head...
        with repo:
            repo.post_status(prx.head, 'success', 'ci/runbot')
            repo.post_status(prx.head, 'success', 'legal/cla')
        env.run_crons()
        assert pr.state == 'validated'

class TestMergeMethod:
    """
    if event['pull_request']['commits'] == 1, "squash" (/rebase); otherwise
    regular merge
    """
    def test_pr_single_commit(self, repo, env, config):
        """ If single commit, default to rebase & FF
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
            repo.make_ref('heads/master', m2)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).squash

        env.run_crons()
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).staging_id

        staging = repo.commit('heads/staging.master')
        assert not repo.is_ancestor(prx.head, of=staging.id),\
            "the pr head should not be an ancestor of the staging branch in a squash merge"
        assert repo.read_tree(staging) == {
            'm': 'c1', 'm2': 'm2',
        }, "the tree should still be correctly merged"
        assert staging.parents == [m2],\
            "dummy commit aside, the previous master's tip should be the sole parent of the staging commit"

        with repo:
            repo.post_status(staging.id, 'success', 'legal/cla')
            repo.post_status(staging.id, 'success', 'ci/runbot')
        env.run_crons()
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])
        assert pr.state == 'merged'
        assert prx.state == 'closed'
        assert json.loads(pr.commits_map) == {
            c1: staging.id,
            '': staging.id,
        }, "for a squash, the one PR commit should be mapped to the one rebased commit"

    def test_delegate_method(self, repo, env, users, config):
        """Delegates should be able to configure the merge method.
        """
        with repo:
            m, _ = repo.make_commits(
                None,
                Commit('initial', tree={'m': 'm'}),
                Commit('second', tree={'m2': 'm2'}),
                ref="heads/master"
            )

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            pr = repo.make_pr(target='master', head=c1)
            repo.post_status(pr.head, 'success', 'legal/cla')
            repo.post_status(pr.head, 'success', 'ci/runbot')
            pr.post_comment('hansen delegate+', config['role_reviewer']['token'])
            pr.post_comment('hansen merge', config['role_user']['token'])
        env.run_crons()

        assert pr.user == users['user']
        assert to_pr(env, pr).merge_method == 'merge'

    def test_pr_update_to_many_commits(self, repo, env):
        """
        If a PR starts with 1 commit and a second commit is added, the PR
        should be unflagged as squash
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
            repo.make_ref('heads/master', m2)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.squash, "a PR with a single commit should be squashed"

        with repo:
            repo.make_commit(prx.ref, 'second2', None, tree={'m': 'c2'})
        assert not pr.squash, "a PR with a single commit should not be squashed"

    def test_pr_reset_to_single_commit(self, repo, env):
        """
        If a PR starts at >1 commits and is reset back to 1, the PR should be
        re-flagged as squash
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
            repo.make_ref('heads/master', m2)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            c2 = repo.make_commit(c1, 'second2', None, tree={'m': 'c2'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c2)
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        pr.merge_method = 'rebase-merge'
        assert not pr.squash, "a PR with a single commit should not be squashed"

        with repo:
            repo.update_ref(
                prx.ref,
                repo.make_commit(m, 'fixup', None, tree={'m': 'c2'}),
                force=True
            )
        assert pr.squash, "a PR with a single commit should be squashed"
        assert not pr.merge_method, \
            "resetting a PR to a single commit should remove the merge method"

    def test_pr_no_method(self, repo, env, users, config):
        """ a multi-repo PR should not be staged by default, should also get
        feedback indicating a merge method is necessary
        """
        with repo:
            _, m1, _ = repo.make_commits(
                None,
                Commit('M0', tree={'m': '0'}),
                Commit('M1', tree={'m': '1'}),
                Commit('M2', tree={'m': '2'}),
                ref='heads/master'
            )

            _, b1 = repo.make_commits(
                m1,
                Commit('B0', tree={'b': '0'}),
                Commit('B1', tree={'b': '1'}),
            )
            prx = repo.make_pr(title='title', body='body', target='master', head=b1)
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        assert not to_pr(env, prx).staging_id

        assert prx.comments == [
            (users['reviewer'], 'hansen r+'),
            seen(env, prx, users),
            (users['user'], """@{user} @{reviewer} because this PR has multiple \
commits, I need to know how to merge it:

* `merge` to merge directly, using the PR as merge commit message
* `rebase-merge` to rebase and merge, using the PR as merge commit message
* `rebase-ff` to rebase and fast-forward
""".format_map(users)),
        ]

    def test_pr_method_no_review(self, repo, env, users, config):
        """ Configuring the method should be independent from the review
        """
        with repo:
            m0 = repo.make_commit(None, 'M0', None, tree={'m': '0'})
            m1 = repo.make_commit(m0, 'M1', None, tree={'m': '1'})
            m2 = repo.make_commit(m1, 'M2', None, tree={'m': '2'})
            repo.make_ref('heads/master', m2)

            b0 = repo.make_commit(m1, 'B0', None, tree={'m': '1', 'b': '0'})
            b1 = repo.make_commit(b0, 'B1', None, tree={'m': '1', 'b': '1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=b1)
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        with repo:
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')

            prx.post_comment('hansen rebase-merge', config['role_reviewer']['token'])
        assert pr.merge_method == 'rebase-merge'
        env.run_crons()

        with repo:
            prx.post_comment('hansen merge', config['role_reviewer']['token'])
        assert pr.merge_method == 'merge'
        env.run_crons()

        with repo:
            prx.post_comment('hansen rebase-ff', config['role_reviewer']['token'])
        assert pr.merge_method == 'rebase-ff'
        env.run_crons()

        assert prx.comments == [
            (users['reviewer'], 'hansen rebase-merge'),
            seen(env, prx, users),
            (users['user'], "Merge method set to rebase and merge, using the PR as merge commit message."),
            (users['reviewer'], 'hansen merge'),
            (users['user'], "Merge method set to merge directly, using the PR as merge commit message."),
            (users['reviewer'], 'hansen rebase-ff'),
            (users['user'], "Merge method set to rebase and fast-forward."),
        ]

    def test_pr_rebase_merge(self, repo, env, users, config):
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
        with repo:
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
            prx = repo.make_pr(title='title', body='body', target='master', head=b1)
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
        env.run_crons()

        pr_id = to_pr(env, prx)
        # create a dag (msg:str, parents:set) from the log
        staging = log_to_node(repo.log('heads/staging.master'))
        # then compare to the dag version of the right graph
        nm2 = node('M2', node('M1', node('M0')))
        nb1 = node(part_of('B1', pr_id), node(part_of('B0', pr_id), nm2))
        reviewer = get_partner(env, users["reviewer"]).formatted_email
        merge_head = (
            f'title\n\nbody\n\ncloses {pr_id.display_name}\n\nSigned-off-by: {reviewer}\n',
            frozenset([nm2, nb1])
        )
        assert staging == merge_head
        st = pr_id.staging_id
        assert st

        with repo: prx.title = 'title 2'
        assert not pr_id.staging_id, "updating the message of a merge-staged PR should unstage rien"
        assert st.reason == f'{pr_id.display_name} merge message updated'
        # since we updated the description, the merge_head value is impacted,
        # and it's checked again later on
        merge_head = (
            merge_head[0].replace('title', 'title 2'),
            merge_head[1],
        )
        env.run_crons()
        assert pr_id.staging_id, "PR should immediately be re-stageable"

        with repo:
            repo.post_status('heads/staging.master', 'success', 'legal/cla')
            repo.post_status('heads/staging.master', 'success', 'ci/runbot')
        env.run_crons()

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.state == 'merged'

        # check that the dummy commit is not in the final master
        master = log_to_node(repo.log('heads/master'))
        assert master == merge_head
        head = repo.commit('heads/master')
        final_tree = repo.read_tree(head)
        assert final_tree == {'m': '2', 'b': '1'}, "sanity check of final tree"
        r1 = repo.commit(head.parents[1])
        r0 = repo.commit(r1.parents[0])
        assert json.loads(pr.commits_map) == {
            b0: r0.id,
            b1: r1.id,
            '': head.id,
        }
        assert r0.parents == [m2]

    def test_pr_rebase_ff(self, repo, env, users, config):
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
        with repo:
            _, m1, m2 = repo.make_commits(
                None,
                Commit('M0', tree={'m': '0'}),
                Commit('M1', tree={'m': '1'}),
                Commit('M2', tree={'m': '2'}),
                ref='heads/master'
            )

            b0, b1 = repo.make_commits(
                m1,
                Commit('B0', tree={'b': '0'}, author={'name': 'Maarten Tromp', 'email': 'm.tromp@example.nl', 'date': '1651-03-30T12:00:00Z'}),
                Commit('B1', tree={'b': '1'}, author={'name': 'Rein Huydecoper', 'email': 'r.huydecoper@example.nl', 'date': '1986-04-17T12:00:00Z'}),
            )

            prx = repo.make_pr(title='title', body='body', target='master', head=b1)
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])
        env.run_crons()

        pr_id = to_pr(env, prx)
        # create a dag (msg:str, parents:set) from the log
        staging = log_to_node(repo.log('heads/staging.master'))
        # then compare to the dag version of the right graph
        nm2 = node('M2', node('M1', node('M0')))
        reviewer = get_partner(env, users["reviewer"]).formatted_email
        nb1 = node(f'B1\n\ncloses {pr_id.display_name}\n\nSigned-off-by: {reviewer}\n',
                   node(part_of('B0', pr_id), nm2))
        assert staging == nb1

        with repo:
            repo.post_status('heads/staging.master', 'success', 'legal/cla')
            repo.post_status('heads/staging.master', 'success', 'ci/runbot')
        env.run_crons()

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.state == 'merged'

        # check that the dummy commit is not in the final master
        master = log_to_node(repo.log('heads/master'))
        assert master == nb1
        head = repo.commit('heads/master')
        final_tree = repo.read_tree(head)
        assert final_tree == {'m': '2', 'b': '1'}, "sanity check of final tree"

        m1 = head
        m0 = repo.commit(m1.parents[0])
        assert json.loads(pr.commits_map) == {
            '': m1.id, # merge commit
            b1: m1.id, # second PR's commit
            b0: m0.id, # first PR's commit
        }
        assert m0.parents == [m2], "can't hurt to check the parent of our root commit"
        assert m0.author['date'] != m0.committer['date'], "commit date should have been rewritten"
        assert m1.author['date'] != m1.committer['date'], "commit date should have been rewritten"

        utcday = datetime.datetime.utcnow().date()
        def parse(dt):
            return datetime.datetime.strptime(dt, "%Y-%m-%dT%H:%M:%SZ")

        # FIXME: actual commit creation could run before the date rollover and
        #        local datetime.utcnow() after
        assert parse(m0.committer['date']).date() == utcday
        # FIXME: git date storage is unreliable and non-portable outside of an
        #        unsigned 31b epoch range so the m0 event may get flung in the
        #        future (compared to the literal datum), this test unexpectedly
        #        becoming true if run on the exact wrong day
        assert parse(m0.author['date']).date() != utcday
        assert parse(m1.committer['date']).date() == utcday
        assert parse(m0.author['date']).date() != utcday

    @pytest.mark.skip(reason="what do if the PR contains merge commits???")
    def test_pr_contains_merges(self, repo, env):
        pass

    def test_pr_force_merge_single_commit(self, repo, env, users, config):
        """ should be possible to flag a PR as regular-merged, regardless of
        its commits count

        M      M<--+
        ^      ^   |
        |  ->  |   C0
        +      |   ^
        C0     +   |
               gib-+
        """
        with repo:
            m = repo.make_commit(None, "M", None, tree={'a': 'a'})
            repo.make_ref('heads/master', m)

            c0 = repo.make_commit(m, 'C0', None, tree={'a': 'b'})
            prx = repo.make_pr(title="gibberish", body="blahblah", target='master', head=c0)
        env.run_crons('runbot_merge.merge_cron', 'runbot_merge.staging_cron')

        with repo:
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen r+ merge', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('heads/staging.master', 'success', 'ci/runbot')
            repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env.run_crons()

        master = repo.commit('heads/master')
        assert master.parents == [m, prx.head], \
            "master's parents should be the old master & the PR head"

        m = node('M')
        c0 = node('C0', m)
        reviewer = get_partner(env, users["reviewer"]).formatted_email
        expected = node('gibberish\n\nblahblah\n\ncloses {}#{}'
                        '\n\nSigned-off-by: {}\n'.format(repo.name, prx.number, reviewer), m, c0)
        assert log_to_node(repo.log('heads/master')), expected
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert json.loads(pr.commits_map) == {
            prx.head: prx.head,
            '': master.id
        }

    def test_unrebase_emptymessage(self, repo, env, users, config):
        """ When merging between master branches (e.g. forward port), the PR
        may have only a title
        """
        with repo:
            m = repo.make_commit(None, "M", None, tree={'a': 'a'})
            repo.make_ref('heads/master', m)

            c0 = repo.make_commit(m, 'C0', None, tree={'a': 'b'})
            prx = repo.make_pr(title="gibberish", body=None, target='master', head=c0)
        env.run_crons('runbot_merge.merge_cron', 'runbot_merge.staging_cron')

        with repo:
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen r+ merge', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('heads/staging.master', 'success', 'ci/runbot')
            repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env.run_crons()

        master = repo.commit('heads/master')
        assert master.parents == [m, prx.head], \
            "master's parents should be the old master & the PR head"

        m = node('M')
        c0 = node('C0', m)
        reviewer = get_partner(env, users["reviewer"]).formatted_email
        expected = node('gibberish\n\ncloses {}#{}'
                        '\n\nSigned-off-by: {}\n'.format(repo.name, prx.number, reviewer), m, c0)
        assert log_to_node(repo.log('heads/master')), expected

    @pytest.mark.parametrize('separator', [
        '***', '___', '\n---',
        '*'*12, '\n----------------',
        '- - -', '  **     **     **'
    ])
    def test_pr_message_break(self, repo, env, users, config, separator):
        """ If the PR message contains a "thematic break", only the part before
        should be included in the merge commit's message.
        """
        reviewer = get_partner(env, users["reviewer"]).formatted_email
        with repo:
            root = repo.make_commits(None, Commit("root", tree={'a': 'a'}), ref='heads/master')

            repo.make_commits(root, Commit('C', tree={'a': 'b'}), ref='heads/change')
            pr = repo.make_pr(title="title", body=f'first\n{separator}\nsecond',
                              target='master', head='change')
            repo.post_status(pr.head, 'success', 'legal/cla')
            repo.post_status(pr.head, 'success', 'ci/runbot')
            pr.post_comment('hansen r+ merge', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('heads/staging.master', 'success', 'ci/runbot')
            repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env.run_crons()

        head = repo.commit('heads/master')
        assert head.message == f"""\
title

first

closes {repo.name}#{pr.number}

Signed-off-by: {reviewer}
""", "should not contain the content which follows the thematic break"

    def test_pr_message_setex_title(self, repo, env, users, config):
        """ should not break on a proper SETEX-style title """
        reviewer = get_partner(env, users["reviewer"]).formatted_email
        with repo:
            root = repo.make_commits(None, Commit("root", tree={'a': 'a'}), ref='heads/master')

            repo.make_commits(root, Commit('C', tree={'a': 'b'}), ref='heads/change')
            pr = repo.make_pr(title="title", body="""\
Title
---
This is some text

Title 2
-------
This is more text
***
removed
""",
                              target='master', head='change')
            repo.post_status(pr.head, 'success', 'legal/cla')
            repo.post_status(pr.head, 'success', 'ci/runbot')
            pr.post_comment('hansen r+ merge', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('heads/staging.master', 'success', 'ci/runbot')
            repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env.run_crons()

        head = repo.commit('heads/master')
        assert head.message == f"""\
title

Title
---
This is some text

Title 2
-------
This is more text

closes {repo.name}#{pr.number}

Signed-off-by: {reviewer}
""", "should not break the SETEX titles"

    def test_rebase_no_edit(self, repo, env, users, config):
        """ Only the merge messages should be de-breaked
        """
        reviewer = get_partner(env, users["reviewer"]).formatted_email
        with repo:
            root = repo.make_commits(None, Commit("root", tree={'a': 'a'}), ref='heads/master')

            repo.make_commits(root, Commit('Commit\n\nfirst\n***\nsecond', tree={'a': 'b'}), ref='heads/change')
            pr = repo.make_pr(title="PR", body='first\n***\nsecond',
                              target='master', head='change')
            repo.post_status(pr.head, 'success', 'legal/cla')
            repo.post_status(pr.head, 'success', 'ci/runbot')
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('heads/staging.master', 'success', 'ci/runbot')
            repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env.run_crons()

        head = repo.commit('heads/master')
        assert head.message == f"""\
Commit

first
***
second

closes {repo.name}#{pr.number}

Signed-off-by: {reviewer}
""", "squashed / rebased messages should not be stripped"

    def test_title_no_edit(self, repo, env, users, config):
        """The first line of a commit message should not be taken in account for
        rewriting, especially as it can be untagged and interpreted as a
        pseudo-header
        """
        with repo:
            repo.make_commits(None, Commit("0", tree={'a': '1'}), ref='heads/master')
            repo.make_commits(
                'master',
                Commit('Some: thing\n\nis odd', tree={'b': '1'}),
                Commit('thing: thong', tree={'b': '2'}),
                ref='heads/change')

            pr = repo.make_pr(target='master', head='change')
            repo.post_status(pr.head, 'success', 'legal/cla')
            repo.post_status(pr.head, 'success', 'ci/runbot')
            pr.post_comment('hansen rebase-ff r+', config['role_reviewer']['token'])
        env.run_crons()

        pr_id = to_pr(env, pr)
        assert pr_id.staging_id # check PR is staged


        reviewer = get_partner(env, users["reviewer"]).formatted_email
        staging_head = repo.commit('staging.master')
        assert staging_head.message == f"""\
thing: thong

closes {pr_id.display_name}

Signed-off-by: {reviewer}
"""
        assert repo.commit(staging_head.parents[0]).message == f"""\
Some: thing

is odd

Part-of: {pr_id.display_name}
"""

    def test_pr_mergehead(self, repo, env, config):
        """ if the head of the PR is a merge commit and one of the parents is
        in the target, replicate the merge commit instead of merging

        rankdir="BT"
        M2 -> M1
        C0 -> M1
        C1 -> C0
        C1 -> M2

        C1 [label = "\\N / MERGE"]
        """
        with repo:
            m1 = repo.make_commit(None, "M1", None, tree={'a': '0'})
            m2 = repo.make_commit(m1, "M2", None, tree={'a': '1'})
            repo.make_ref('heads/master', m2)

            c0 = repo.make_commit(m1, 'C0', None, tree={'a': '0', 'b': '2'})
            c1 = repo.make_commit([c0, m2], 'C1', None, tree={'a': '1', 'b': '2'})
            prx = repo.make_pr(title="T", body="TT", target='master', head=c1)
        env.run_crons()

        with repo:
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen r+ merge', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('heads/staging.master', 'success', 'ci/runbot')
            repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env.run_crons()

        master = repo.commit('heads/master')
        assert master.parents == [m2, c0]
        m1 = node('M1')
        expected = node('C1', node('C0', m1), node('M2', m1))
        assert log_to_node(repo.log('heads/master')), expected

    def test_pr_mergehead_nonmember(self, repo, env, users, config):
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
        with repo:
            m1 = repo.make_commit(None, "M1", None, tree={'a': '0'})
            m2 = repo.make_commit(m1, "M2", None, tree={'a': '1'})
            repo.make_ref('heads/master', m2)

            b0 = repo.make_commit(m1, 'B0', None, tree={'a': '0', 'bb': 'bb'})

            c0 = repo.make_commit(m1, 'C0', None, tree={'a': '0', 'b': '2'})
            c1 = repo.make_commit([c0, b0], 'C1', None, tree={'a': '0', 'b': '2', 'bb': 'bb'})
            prx = repo.make_pr(title="T", body="TT", target='master', head=c1)
        env.run_crons()

        with repo:
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen r+ merge', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('heads/staging.master', 'success', 'ci/runbot')
            repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env.run_crons()

        master = repo.commit('heads/master')
        assert master.parents == [m2, c1]
        assert repo.read_tree(master) == {'a': '1', 'b': '2', 'bb': 'bb'}

        m1 = node('M1')
        reviewer = get_partner(env, users["reviewer"]).formatted_email
        expected = node(
            'T\n\nTT\n\ncloses {}#{}\n\nSigned-off-by: {}\n'.format(repo.name, prx.number, reviewer),
            node('M2', m1),
            node('C1', node('C0', m1), node('B0', m1))
        )
        assert log_to_node(repo.log('heads/master')), expected

    def test_squash_merge(self, repo, env, config, users):
        other_user = requests.get('https://api.github.com/user', headers={
            'Authorization': 'token %s' % config['role_other']['token'],
        }).json()
        other_user = {
            'name': other_user['name'] or other_user['login'],
            # FIXME: not guaranteed
            'email': other_user['email'] or 'other@example.org',
        }
        a_user = {'name': 'bob', 'email': 'builder@example.org', 'date': '1999-04-12T08:19:30Z'}
        with repo:
            repo.make_commits(None, Commit('initial', tree={'a': '0'}), ref='heads/master')

            repo.make_commits(
                'master',
                Commit('sub', tree={'b': '0'}, committer=a_user),
                ref='heads/other'
            )
            pr1 = repo.make_pr(title='first pr', target='master', head='other')
            repo.post_status('other', 'success', 'legal/cla')
            repo.post_status('other', 'success', 'ci/runbot')

            pr_2_commits = repo.make_commits(
                'master',
                Commit('x', tree={'x': '0'}, author=other_user, committer=a_user),
                Commit('y', tree={'x': '1'}, author=a_user, committer=other_user),
                ref='heads/other2',
            )
            c1, c2 = map(repo.commit, pr_2_commits)
            assert c1.author['name'] != c2.author['name']
            assert c1.committer['name'] != c2.committer['name']
            pr2 = repo.make_pr(title='second pr', target='master', head='other2')
            repo.post_status('other2', 'success', 'legal/cla')
            repo.post_status('other2', 'success', 'ci/runbot')
        env.run_crons()

        with repo: # comments sequencing
            pr1.post_comment('hansen r+ squash', config['role_reviewer']['token'])
            pr2.post_comment('hansen r+ squash', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('staging.master', 'success', 'legal/cla')
            repo.post_status('staging.master', 'success', 'ci/runbot')
        env.run_crons()

        # PR 1 should have merged properly, the PR message should be the
        # message of the merged commit
        pr1_id = to_pr(env, pr1)
        assert pr1_id.state == 'merged'
        assert pr1.comments == [
            seen(env, pr1, users),
            (users['reviewer'], 'hansen r+ squash'),
            (users['user'], 'Merge method set to squash.')
        ]

        pr2_id = to_pr(env, pr2)
        assert pr2_id.state == 'merged'
        assert pr2.comments == [
            seen(env, pr2, users),
            (users['reviewer'], 'hansen r+ squash'),
            (users['user'], 'Merge method set to squash.'),
        ]

        two, one, _root = repo.log('master')

        assert one['commit']['message'] == f"""first pr

closes {pr1_id.display_name}

Signed-off-by: {get_partner(env, users["reviewer"]).formatted_email}
"""
        assert one['commit']['committer']['name'] == a_user['name']
        assert one['commit']['committer']['email'] == a_user['email']
        commit_date = datetime.datetime.strptime(one['commit']['committer']['date'], '%Y-%m-%dT%H:%M:%SZ')
        # using timestamp (and working in seconds) because `pytest.approx`
        # silently fails on datetimes (#8395)
        assert commit_date.timestamp() == pytest.approx(time.time(), abs=5*60), \
            "the commit date of the merged commit should be about now, despite" \
            " the source commit being >20 years old"

        # FIXME: should probably get the token from the project to be sure it's
        #        the bot user
        current_user = repo._session.get('https://api.github.com/user').json()
        current_user = {
            'name': current_user['name'] or current_user['login'],
            # FIXME: not guaranteed
            'email': current_user['email'] or 'user@example.org',
        }
        # since there are two authors & two committers on pr2, the auhor and
        # committer of a squash commit should be reset to the bot's identity
        assert two['commit']['committer']['name'] == current_user['name']
        assert two['commit']['committer']['email'] == current_user['email']
        assert two['commit']['author']['name'] == current_user['name']
        assert two['commit']['author']['email'] == current_user['email']
        assert two['commit']['message'] == f"""second pr

closes {pr2_id.display_name}

Signed-off-by: {get_partner(env, users["reviewer"]).formatted_email}
Co-authored-by: {a_user['name']} <{a_user['email']}>
Co-authored-by: {other_user['name']} <{other_user['email']}>
"""
        assert repo.read_tree(repo.commit(two['sha'])) == {
            'a': '0',
            'b': '0',
            'x': '1',
        }


class TestPRUpdate(object):
    """ Pushing on a PR should update the HEAD except for merged PRs, it
    can have additional effect (see individual tests)
    """
    def test_update_opened(self, env, repo):
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c)
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.head == c
        # alter & push force PR entirely
        with repo:
            c2 = repo.make_commit(m, 'first', None, tree={'m': 'cc'})
            repo.update_ref(prx.ref, c2, force=True)
        assert pr.head == c2

    def test_reopen_update(self, env, repo):
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c)
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        with repo:
            prx.close()
        assert pr.state == 'closed'
        assert pr.head == c

        with repo:
            prx.open()
        assert pr.state == 'opened'

        with repo:
            c2 = repo.make_commit(c, 'first', None, tree={'m': 'cc'})
            repo.update_ref(prx.ref, c2, force=True)
        assert pr.head == c2

    def test_update_validated(self, env, repo):
        """ Should reset to opened
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c)
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
        env.run_crons()
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.head == c
        assert pr.state == 'validated'

        with repo:
            c2 = repo.make_commit(m, 'first', None, tree={'m': 'cc'})
            repo.update_ref(prx.ref, c2, force=True)
        assert pr.head == c2
        assert pr.state == 'opened'

    def test_update_approved(self, env, repo, config):
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c)
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.head == c
        assert pr.state == 'approved'

        with repo:
            c2 = repo.make_commit(c, 'first', None, tree={'m': 'cc'})
            repo.update_ref(prx.ref, c2, force=True)
        assert pr.head == c2
        assert pr.state == 'opened'

    def test_update_ready(self, env, repo, config):
        """ Should reset to opened
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c)
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.head == c
        assert pr.state == 'ready'

        with repo:
            c2 = repo.make_commit(c, 'first', None, tree={'m': 'cc'})
            repo.update_ref(prx.ref, c2, force=True)
        assert pr.head == c2
        assert pr.state == 'opened'

    def test_update_staged(self, env, repo, config):
        """ Should cancel the staging & reset PR to opened
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c)
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        env.run_crons()
        assert pr.state == 'ready'
        assert pr.staging_id

        with repo:
            c2 = repo.make_commit(c, 'first', None, tree={'m': 'cc'})
            repo.update_ref(prx.ref, c2, force=True)
        assert pr.head == c2
        assert pr.state == 'opened'
        assert not pr.staging_id
        assert not env['runbot_merge.stagings'].search([])

    def test_split(self, env, repo, config):
        """ Should remove the PR from its split, and possibly delete the split
        entirely.
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'first', None, tree={'m': 'm', '1': '1'})
            repo.make_ref('heads/p1', c)
            prx1 = repo.make_pr(title='t1', body='b1', target='master', head='p1')
            repo.post_status(prx1.head, 'success', 'legal/cla')
            repo.post_status(prx1.head, 'success', 'ci/runbot')
            prx1.post_comment('hansen r+', config['role_reviewer']['token'])

            c = repo.make_commit(m, 'first', None, tree={'m': 'm', '2': '2'})
            repo.make_ref('heads/p2', c)
            prx2 = repo.make_pr(title='t2', body='b2', target='master', head='p2')
            repo.post_status(prx2.head, 'success', 'legal/cla')
            repo.post_status(prx2.head, 'success', 'ci/runbot')
            prx2.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        pr1, pr2 = env['runbot_merge.pull_requests'].search([], order='number')
        assert pr1.number == prx1.number
        assert pr2.number == prx2.number
        assert pr1.staging_id == pr2.staging_id
        s0 = pr1.staging_id

        with repo:
            repo.post_status('heads/staging.master', 'failure', 'ci/runbot')
        env.run_crons()

        assert pr1.staging_id and pr1.staging_id != s0, "pr1 should have been re-staged"
        assert not pr2.staging_id, "pr2 should not"
        # TODO: remote doesn't currently handle env context so can't mess
        #       around using active_test=False
        assert env['runbot_merge.split'].search([])

        with repo:
            repo.update_ref(prx2.ref, repo.make_commit(c, 'second', None, tree={'m': 'm', '2': '22'}), force=True)
        # probably not necessary ATM but...
        env.run_crons()

        assert pr2.state == 'opened', "state should have been reset"
        assert not env['runbot_merge.split'].search([]), "there should be no split left"

    def test_update_error(self, env, repo, config):
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c)
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        env.run_crons()
        assert pr.state == 'ready'
        assert pr.staging_id

        h = repo.commit('heads/staging.master').id
        with repo:
            repo.post_status(h, 'success', 'legal/cla')
            repo.post_status(h, 'failure', 'ci/runbot')
        env.run_crons()
        assert not pr.staging_id
        assert pr.state == 'error'

        with repo:
            c2 = repo.make_commit(c, 'first', None, tree={'m': 'cc'})
            repo.update_ref(prx.ref, c2, force=True)
        assert pr.head == c2
        assert pr.state == 'opened'

    def test_unknown_pr(self, env, repo):
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/1.0', m)

            c = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='1.0', head=c)
        assert not env['runbot_merge.pull_requests'].search([('number', '=', prx.number)])

        env['runbot_merge.project'].search([]).write({
            'branch_ids': [(0, 0, {'name': '1.0'})]
        })

        with repo:
            c2 = repo.make_commit(c, 'second', None, tree={'m': 'c2'})
            repo.update_ref(prx.ref, c2, force=True)

        assert not env['runbot_merge.pull_requests'].search([('number', '=', prx.number)])

    def test_update_to_ci(self, env, repo):
        """ If a PR is updated to a known-valid commit, it should be
        validated
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
            c2 = repo.make_commit(m, 'first', None, tree={'m': 'cc'})
            repo.post_status(c2, 'success', 'legal/cla')
            repo.post_status(c2, 'success', 'ci/runbot')
        env.run_crons()

        with repo:
            prx = repo.make_pr(title='title', body='body', target='master', head=c)
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.head == c
        assert pr.state == 'opened'

        with repo:
            repo.update_ref(prx.ref, c2, force=True)
        assert pr.head == c2
        assert pr.state == 'validated'

    def test_update_missed(self, env, repo, config, users):
        """ Sometimes github's webhooks don't trigger properly, a branch's HEAD
        does not get updated and we might e.g. attempt to merge a PR despite it
        now being unreviewed or failing CI or somesuch.

        Therefore during the staging process we should check what we can, reject
        the staging if cricical properties were found to mismatch, and notify
        the pull request.

        The PR should then be reset to open (and transition to validated on its
        own if the existing or new head has valid statuses), we don't want to
        put it in an error state as technically there's no error, just something
        which went a bit weird.
        """
        with repo:
            [c] = repo.make_commits(None, repo.Commit('m', tree={'a': '0'}), ref='heads/master')
            repo.make_ref('heads/somethingelse', c)

            [c] = repo.make_commits(
                'heads/master', repo.Commit('title \n\nbody', tree={'a': '1'}), ref='heads/abranch')
            pr = repo.make_pr(target='master', head='abranch')
            repo.post_status(pr.head, 'success', 'legal/cla')
            repo.post_status(pr.head, 'success', 'ci/runbot')
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
        pr_id = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', pr.number),
        ])
        env.run_crons('runbot_merge.process_updated_commits')
        assert pr_id.message == 'title\n\nbody'
        assert pr_id.state == 'ready'

        # TODO: find way to somehow skip / ignore the update_ref?
        with repo:
            # can't push a second commit because then the staging crashes due
            # to the PR *actually* having more than 1 commit and thus needing
            # a configuration
            [c2] = repo.make_commits('heads/master', repo.Commit('c2', tree={'a': '2'}))
            repo.post_status(c2, 'success', 'legal/cla')
            repo.post_status(c2, 'success', 'ci/runbot')
            repo.update_ref(pr.ref, c2, force=True)

        other = env['runbot_merge.branch'].create({
            'name': 'somethingelse',
            'project_id': env['runbot_merge.project'].search([]).id,
        })

        # we missed the update notification so the db should still be at c and
        # in a "ready" state
        pr_id.write({
            'head': c,
            'state': 'ready',
            'message': "Something else",
            'target': other.id,
        })

        env.run_crons()

        # the PR should not get merged, and should be updated
        assert pr_id.state == 'validated'
        assert pr_id.head == c2
        assert pr_id.message == 'title\n\nbody'
        assert pr_id.target.name == 'master'
        assert pr.comments[-1]['body'] == """\
@{} @{} we apparently missed updates to this PR and tried to stage it in a state \
which might not have been approved.

The properties Head, Target, Message were not correctly synchronized and have been updated.

<details><summary>differences</summary>

```diff
  Head:
- {}
+ {}
  
  Target branch:
- somethingelse
+ master
  
  Message:
- Something else
+ title
  
+ body
+ 
```
</details>

Note that we are unable to check the properties Merge Method, Overrides, Draft.

Please check and re-approve.
""".format(users['user'], users['reviewer'], c, c2)

        # if the head commit doesn't change, that part should still be valid
        with repo:
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
        assert pr_id.state == 'ready'
        pr_id.write({'message': 'wrong'})
        env.run_crons()

        assert pr_id.message == 'title\n\nbody'
        assert pr_id.state == 'validated'
        assert pr.comments[-1]['body'] == """\
@{} @{} we apparently missed updates to this PR and tried to stage it in a state \
which might not have been approved.

The properties Message were not correctly synchronized and have been updated.

<details><summary>differences</summary>

```diff
  Message:
- wrong
+ title
  
+ body
+ 
```
</details>

Note that we are unable to check the properties Merge Method, Overrides, Draft.

Please check and re-approve.
""".format(users['user'], users['reviewer'])

        pr_id.write({
            'head': c,
            'state': 'ready',
            'message': "Something else",
            'target': other.id,
            'draft': True,
        })
        with repo:
            pr.post_comment('hansen check')
        env.run_crons()
        assert pr_id.state == 'validated'
        assert pr_id.head == c2
        assert pr_id.message == 'title\n\nbody' # the commit's message was used for the PR
        assert pr_id.target.name == 'master'
        assert not pr_id.draft
        assert pr.comments[-1] == (
            users['user'],
            f"Updated target, squash, message. Updated {pr_id.display_name} to ready. Updated to {c2}."
        )

    def test_update_closed(self, env, repo):
        with repo:
            [m] = repo.make_commits(None, repo.Commit('initial', tree={'m': 'm'}), ref='heads/master')

            [c] = repo.make_commits(m, repo.Commit('first', tree={'m': 'm3'}), ref='heads/abranch')
            prx = repo.make_pr(title='title', body='body', target='master', head=c)
        env.run_crons()
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])
        assert pr.state == 'opened'
        assert pr.head == c
        assert pr.squash

        with repo:
            prx.close()

        with repo:
            c2 = repo.make_commit(c, 'xxx', None, tree={'m': 'm4'})
            repo.update_ref(prx.ref, c2)

        assert pr.state == 'closed'
        assert pr.head == c
        assert pr.squash

        with repo:
            prx.open()
        assert pr.state == 'opened'
        assert pr.head == c2
        assert not pr.squash

    def test_update_closed_revalidate(self, env, repo):
        """ The PR should be validated on opening and reopening in case there's
        already a CI+ stored (as the CI might never trigger unless explicitly
        re-requested)
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'fist', None, tree={'m': 'c1'})
            repo.post_status(c, 'success', 'legal/cla')
            repo.post_status(c, 'success', 'ci/runbot')
            prx = repo.make_pr(title='title', body='body', target='master', head=c)

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.state == 'validated', \
            "if a PR is created on a CI'd commit, it should be validated immediately"

        with repo: prx.close()
        assert pr.state == 'closed'

        with repo: prx.open()
        assert pr.state == 'validated', \
            "if a PR is reopened and had a CI'd head, it should be validated immediately"

    @pytest.mark.xfail(reason="github doesn't allow reopening force-pushed PRs", strict=True)
    def test_force_update_closed(self, env, repo):
        with repo:
            [m] = repo.make_commits(None, repo.Commit('initial', tree={'m': 'm'}), ref='heads/master')

            [c] = repo.make_commits(m, repo.Commit('first', tree={'m': 'm3'}), ref='heads/abranch')
            prx = repo.make_pr(title='title', body='body', target='master', head=c)
        env.run_crons()
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])
        with repo:
            prx.close()

        with repo:
            c2 = repo.make_commit(m, 'xxx', None, tree={'m': 'm4'})
            repo.update_ref(prx.ref, c2, force=True)

        with repo:
            prx.open()
        assert pr.head == c2

class TestBatching(object):
    def _pr(self, repo, prefix, trees, *, target='master', user, reviewer,
            statuses=(('ci/runbot', 'success'), ('legal/cla', 'success'))
        ):
        """ Helper creating a PR from a series of commits on a base
        """
        *_, c = repo.make_commits(
            'heads/{}'.format(target),
            *(
                repo.Commit('commit_{}_{:02}'.format(prefix, i), tree=t)
                for i, t in enumerate(trees)
            ),
            ref='heads/{}'.format(prefix)
        )
        pr = repo.make_pr(title='title {}'.format(prefix), body='body {}'.format(prefix),
                          target=target, head=prefix, token=user)

        for context, result in statuses:
            repo.post_status(c, result, context)
        if reviewer:
            pr.post_comment(
                'hansen r+%s' % (' rebase-merge' if len(trees) > 1 else ''),
                reviewer
            )
        return pr

    def test_staging_batch(self, env, repo, users, config):
        """ If multiple PRs are ready for the same target at the same point,
        they should be staged together
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
            repo.make_ref('heads/master', m)

            pr1 = self._pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
            pr2 = self._pr(repo, 'PR2', [{'c': 'CCC'}, {'d': 'DDD'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        env.run_crons()

        pr1 = to_pr(env, pr1)
        assert pr1.staging_id
        pr2 = to_pr(env, pr2)
        assert pr1.staging_id
        assert pr2.staging_id
        assert pr1.staging_id == pr2.staging_id

        log = list(repo.log('heads/staging.master'))
        staging = log_to_node(log)
        reviewer = get_partner(env, users["reviewer"]).formatted_email
        p1 = node(
            'title PR1\n\nbody PR1\n\ncloses {}\n\nSigned-off-by: {}\n'.format(pr1.display_name, reviewer),
            node('initial'),
            node(part_of('commit_PR1_01', pr1), node(part_of('commit_PR1_00', pr1), node('initial')))
        )
        p2 = node(
            'title PR2\n\nbody PR2\n\ncloses {}\n\nSigned-off-by: {}\n'.format(pr2.display_name, reviewer),
            p1,
            node(part_of('commit_PR2_01', pr2), node(part_of('commit_PR2_00', pr2), p1))
        )
        assert staging == p2

    def test_staging_batch_norebase(self, env, repo, users, config):
        """ If multiple PRs are ready for the same target at the same point,
        they should be staged together
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
            repo.make_ref('heads/master', m)

            pr1 = self._pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
            pr1.post_comment('hansen merge', config['role_reviewer']['token'])
            pr2 = self._pr(repo, 'PR2', [{'c': 'CCC'}, {'d': 'DDD'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
            pr2.post_comment('hansen merge', config['role_reviewer']['token'])
        env.run_crons()

        pr1 = to_pr(env, pr1)
        assert pr1.staging_id
        assert pr1.merge_method == 'merge'
        pr2 = to_pr(env, pr2)
        assert pr2.merge_method == 'merge'
        assert pr1.staging_id
        assert pr2.staging_id
        assert pr1.staging_id == pr2.staging_id

        log = list(repo.log('staging.master'))

        staging = log_to_node(log)
        reviewer = get_partner(env, users["reviewer"]).formatted_email

        p1 = node(
            'title PR1\n\nbody PR1\n\ncloses {}#{}\n\nSigned-off-by: {}\n'.format(repo.name, pr1.number, reviewer),
            node('initial'),
            node('commit_PR1_01', node('commit_PR1_00', node('initial')))
        )
        p2 = node(
            'title PR2\n\nbody PR2\n\ncloses {}#{}\n\nSigned-off-by: {}\n'.format(repo.name, pr2.number, reviewer),
            p1,
            node('commit_PR2_01', node('commit_PR2_00', node('initial')))
        )
        assert staging == p2

    def test_staging_batch_squash(self, env, repo, users, config):
        """ If multiple PRs are ready for the same target at the same point,
        they should be staged together
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
            repo.make_ref('heads/master', m)

            pr1 = self._pr(repo, 'PR1', [{'a': 'AAA'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
            pr2 = self._pr(repo, 'PR2', [{'c': 'CCC'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        env.run_crons()

        pr1 = to_pr(env, pr1)
        assert pr1.staging_id
        pr2 = to_pr(env, pr2)
        assert pr1.staging_id
        assert pr2.staging_id
        assert pr1.staging_id == pr2.staging_id

        log = list(repo.log('heads/staging.master'))

        staging = log_to_node(log)
        reviewer = get_partner(env, users["reviewer"]).formatted_email
        expected = node('commit_PR2_00\n\ncloses {}#{}\n\nSigned-off-by: {}\n'.format(repo.name, pr2.number, reviewer),
             node('commit_PR1_00\n\ncloses {}#{}\n\nSigned-off-by: {}\n'.format(repo.name, pr1.number, reviewer),
                  node('initial')))
        assert staging == expected

    def test_batching_pressing(self, env, repo, config):
        """ "Pressing" PRs should be selected before normal & batched together
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
            repo.make_ref('heads/master', m)

            pr21 = self._pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
            pr22 = self._pr(repo, 'PR2', [{'c': 'CCC'}, {'d': 'DDD'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])

            pr11 = self._pr(repo, 'Pressing1', [{'x': 'x'}, {'y': 'y'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
            pr12 = self._pr(repo, 'Pressing2', [{'z': 'z'}, {'zz': 'zz'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
            pr11.post_comment('hansen priority=1', config['role_reviewer']['token'])
            pr12.post_comment('hansen priority=1', config['role_reviewer']['token'])

        pr21, pr22, pr11, pr12 = prs = [to_pr(env, pr) for pr in [pr21, pr22, pr11, pr12]]
        assert pr21.priority == pr22.priority == 2
        assert pr11.priority == pr12.priority == 1

        env.run_crons()

        assert all(pr.state == 'ready' for pr in prs)
        assert not pr21.staging_id
        assert not pr22.staging_id
        assert pr11.staging_id
        assert pr12.staging_id
        assert pr11.staging_id == pr12.staging_id

    def test_batching_urgent(self, env, repo, config):
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
            repo.make_ref('heads/master', m)

            pr21 = self._pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
            pr22 = self._pr(repo, 'PR2', [{'c': 'CCC'}, {'d': 'DDD'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])

            pr11 = self._pr(repo, 'Pressing1', [{'x': 'x'}, {'y': 'y'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
            pr12 = self._pr(repo, 'Pressing2', [{'z': 'z'}, {'zz': 'zz'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
            pr11.post_comment('hansen priority=1', config['role_reviewer']['token'])
            pr12.post_comment('hansen priority=1', config['role_reviewer']['token'])

        # stage PR1
        env.run_crons()
        p_11, p_12, p_21, p_22 = \
            [to_pr(env, pr) for pr in [pr11, pr12, pr21, pr22]]
        assert not p_21.staging_id or p_22.staging_id
        assert p_11.staging_id and p_12.staging_id
        assert p_11.staging_id == p_12.staging_id
        staging_1 = p_11.staging_id

        # no statuses run on PR0s
        with repo:
            pr01 = self._pr(repo, 'Urgent1', [{'n': 'n'}, {'o': 'o'}], user=config['role_user']['token'], reviewer=None, statuses=[])
            pr01.post_comment('hansen priority=0 rebase-merge', config['role_reviewer']['token'])
        p_01 = to_pr(env, pr01)
        assert p_01.state == 'opened'
        assert p_01.priority == 0

        env.run_crons()
        # first staging should be cancelled and PR0 should be staged
        # regardless of CI (or lack thereof)
        assert not staging_1.active
        assert not p_11.staging_id and not p_12.staging_id
        assert p_01.staging_id

    def test_batching_urgenter_than_split(self, env, repo, config):
        """ p=0 PRs should take priority over split stagings (processing
        of a staging having CI-failed and being split into sub-stagings)
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
            repo.make_ref('heads/master', m)

            pr1 = self._pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
            pr2 = self._pr(repo, 'PR2', [{'a': 'some content', 'c': 'CCC'}, {'d': 'DDD'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        env.run_crons()

        p_1 = to_pr(env, pr1)
        p_2 = to_pr(env, pr2)
        st = env['runbot_merge.stagings'].search([])

        # both prs should be part of the staging
        assert st.mapped('batch_ids.prs') == p_1 | p_2

        # add CI failure
        with repo:
            repo.post_status('heads/staging.master', 'failure', 'ci/runbot')
            repo.post_status('heads/staging.master', 'success', 'legal/cla')
        env.run_crons()

        # should have staged the first half
        assert p_1.staging_id.heads
        assert not p_2.staging_id.heads

        # during restaging of pr1, create urgent PR
        with repo:
            pr0 = self._pr(repo, 'urgent', [{'a': 'a', 'b': 'b'}], user=config['role_user']['token'], reviewer=None, statuses=[])
            pr0.post_comment('hansen priority=0', config['role_reviewer']['token'])
        env.run_crons()

        # TODO: maybe just deactivate stagings instead of deleting them when canceling?
        assert not p_1.staging_id
        assert to_pr(env, pr0).staging_id

    def test_urgent_failed(self, env, repo, config):
        """ Ensure pr[p=0,state=failed] don't get picked up
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
            repo.make_ref('heads/master', m)

            pr21 = self._pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])

        p_21 = to_pr(env, pr21)

        # no statuses run on PR0s
        with repo:
            pr01 = self._pr(repo, 'Urgent1', [{'n': 'n'}, {'o': 'o'}], user=config['role_user']['token'], reviewer=None, statuses=[])
            pr01.post_comment('hansen priority=0', config['role_reviewer']['token'])
        p_01 = to_pr(env, pr01)
        p_01.state = 'error'

        env.run_crons()
        assert not p_01.staging_id, "p_01 should not be picked up as it's failed"
        assert p_21.staging_id, "p_21 should have been staged"

    @pytest.mark.skip(reason="Maybe nothing to do, the PR is just skipped and put in error?")
    def test_batching_merge_failure(self):
        pass

    def test_staging_ci_failure_batch(self, env, repo, config):
        """ on failure split batch & requeue
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'a': 'some content'})
            repo.make_ref('heads/master', m)

            pr1 = self._pr(repo, 'PR1', [{'a': 'AAA'}, {'b': 'BBB'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
            pr2 = self._pr(repo, 'PR2', [{'a': 'some content', 'c': 'CCC'}, {'d': 'DDD'}], user=config['role_user']['token'], reviewer=config['role_reviewer']['token'])
        env.run_crons()

        st = env['runbot_merge.stagings'].search([])
        # both prs should be part of the staging
        assert len(st.mapped('batch_ids.prs')) == 2
        # add CI failure
        with repo:
            repo.post_status('heads/staging.master', 'failure', 'ci/runbot')
            repo.post_status('heads/staging.master', 'success', 'legal/cla')

        pr1 = env['runbot_merge.pull_requests'].search([('number', '=', pr1.number)])
        pr2 = env['runbot_merge.pull_requests'].search([('number', '=', pr2.number)])

        env.run_crons()
        # should have split the existing batch into two, with one of the
        # splits having been immediately restaged
        st = env['runbot_merge.stagings'].search([])
        assert len(st) == 1
        assert pr1.staging_id and pr1.staging_id == st

        sp = env['runbot_merge.split'].search([])
        assert len(sp) == 1

        # This is the failing PR!
        h = repo.commit('heads/staging.master').id
        with repo:
            repo.post_status(h, 'failure', 'ci/runbot')
            repo.post_status(h, 'success', 'legal/cla')
        env.run_crons()
        assert pr1.state == 'error'

        assert pr2.staging_id

        h = repo.commit('heads/staging.master').id
        with repo:
            repo.post_status(h, 'success', 'ci/runbot')
            repo.post_status(h, 'success', 'legal/cla')
        env.run_crons('runbot_merge.process_updated_commits', 'runbot_merge.merge_cron', 'runbot_merge.staging_cron')
        assert pr2.state == 'merged'

class TestReviewing(object):
    def test_reviewer_rights(self, env, repo, users, config):
        """Only users with review rights will have their r+ (and other
        attributes) taken in account
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
            repo.make_ref('heads/master', m2)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen r+', config['role_other']['token'])
        env.run_crons()

        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'validated'
        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'ready'
        # second r+ to check warning
        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])

        env.run_crons()
        assert prx.comments == [
            (users['other'], 'hansen r+'),
            seen(env, prx, users),
            (users['user'], "I'm sorry, @{}. I'm afraid I can't do that.".format(users['other'])),
            (users['reviewer'], 'hansen r+'),
            (users['reviewer'], 'hansen r+'),
            (users['user'], "I'm sorry, @{}: this PR is already reviewed, reviewing it again is useless.".format(
                 users['reviewer'])),
        ]

    def test_self_review_fail(self, env, repo, users, config):
        """ Normal reviewers can't self-review
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
            repo.make_ref('heads/master', m2)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c1, token=config['role_reviewer']['token'])
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        assert prx.user == users['reviewer']
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'validated'

        env.run_crons()
        assert prx.comments == [
            (users['reviewer'], 'hansen r+'),
            seen(env, prx, users),
            (users['user'], "I'm sorry, @{}: you can't review+.".format(users['reviewer'])),
        ]

    def test_self_review_success(self, env, repo, users, config):
        """ Some users are allowed to self-review
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
            repo.make_ref('heads/master', m2)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c1, token=config['role_self_reviewer']['token'])
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen r+', config['role_self_reviewer']['token'])
        env.run_crons()

        assert prx.user == users['self_reviewer']
        assert env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ]).state == 'ready'

    def test_delegate_review(self, env, repo, users, config):
        """Users should be able to delegate review to either the creator of
        the PR or an other user without review rights
        """
        env['res.partner'].create({
            'name': users['user'],
            'github_login': users['user'],
            'email': users['user'] + '@example.org',
        })
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
            repo.make_ref('heads/master', m2)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen delegate+', config['role_reviewer']['token'])
            prx.post_comment('hansen r+', config['role_user']['token'])
        env.run_crons()

        assert prx.user == users['user']
        assert to_pr(env, prx).state == 'ready'

    def test_delegate_review_thirdparty(self, env, repo, users, config):
        """Users should be able to delegate review to either the creator of
        the PR or an other user without review rights
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
            repo.make_ref('heads/master', m2)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            # flip case to check that github login is case-insensitive
            other = ''.join(c.lower() if c.isupper() else c.upper() for c in users['other'])
            prx.post_comment('hansen delegate=%s' % other, config['role_reviewer']['token'])
        env.run_crons()
        env['res.partner'].search([('github_login', '=', other)]).email = f'{other}@example.org'

        with repo:
            # check this is ignored
            prx.post_comment('hansen r+', config['role_user']['token'])
        assert prx.user == users['user']
        prx_id = to_pr(env, prx)
        assert prx_id.state == 'validated'

        with repo:
            # check this works
            prx.post_comment('hansen r+', config['role_other']['token'])
        assert prx_id.state == 'ready'

    def test_delegate_prefixes(self, env, repo, config):
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'first', None, tree={'m': 'c'})
            prx = repo.make_pr(title='title', body=None, target='master', head=c)
            prx.post_comment('hansen delegate=foo,@bar,#baz', config['role_reviewer']['token'])

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])

        assert {d.github_login for d in pr.delegates} == {'foo', 'bar', 'baz'}

    def test_actual_review(self, env, repo, config):
        """ treat github reviews as regular comments
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
            repo.make_ref('heads/master', m2)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])

        with repo:
            prx.post_review('COMMENT', "hansen priority=1", config['role_reviewer']['token'])
        assert pr.priority == 1
        assert pr.state == 'opened'

        with repo:
            prx.post_review('APPROVE', "hansen priority=2", config['role_reviewer']['token'])
        assert pr.priority == 2
        assert pr.state == 'opened'

        with repo:
            prx.post_review('REQUEST_CHANGES', 'hansen priority=1', config['role_reviewer']['token'])
        assert pr.priority == 1
        assert pr.state == 'opened'

        with repo:
            prx.post_review('COMMENT', 'hansen r+', config['role_reviewer']['token'])
        assert pr.priority == 1
        assert pr.state == 'approved'

    def test_no_email(self, env, repo, users, config, partners):
        """A review should be rejected if the reviewer doesn't have an email
        configured, otherwise the email address will show up
        @users.noreply.github.com which is *weird*.
        """
        with repo:
            [m] = repo.make_commits(
                None,
                Commit('initial', tree={'m': '1'}),
                ref='heads/master'
            )
            [c] = repo.make_commits(m, Commit('first', tree={'m': '2'}))
            pr = repo.make_pr(target='master', head=c)
        env.run_crons()
        with repo:
            pr.post_comment('hansen delegate+', config['role_reviewer']['token'])
            pr.post_comment('hansen r+', config['role_user']['token'])
        env.run_crons()

        user_partner = env['res.partner'].search([('github_login', '=', users['user'])])
        assert user_partner.email is False
        assert pr.comments == [
            seen(env, pr, users),
            (users['reviewer'], 'hansen delegate+'),
            (users['user'], 'hansen r+'),
            (users['user'], f"I'm sorry, @{users['user']}: I must know your email before you can review PRs. Please contact an administrator."),
        ]
        user_partner.fetch_github_email()
        assert user_partner.email
        with repo:
            pr.post_comment('hansen r+', config['role_user']['token'])
        env.run_crons()
        assert to_pr(env, pr).state == 'approved'


class TestUnknownPR:
    """ Sync PRs initially looked excellent but aside from the v4 API not
    being stable yet, it seems to have greatly regressed in performances to
    the extent that it's almost impossible to sync odoo/odoo today: trying to
    fetch more than 2 PRs per query will fail semi-randomly at one point, so
    fetching all 15000 PRs takes hours

    => instead, create PRs on the fly when getting notifications related to
       valid but unknown PRs
    """
    def test_rplus_unknown(self, repo, env, config, users):
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
            repo.make_ref('heads/master', m2)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot', target_url="http://example.org/wheee")
        env.run_crons()

        # assume an unknown but ready PR: we don't know the PR or its head commit
        env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ]).unlink()
        env['runbot_merge.commit'].search([('sha', '=', prx.head)]).unlink()

        # reviewer reviewers
        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        with repo:
            prx.post_review('REQUEST_CHANGES', 'hansen r-', config['role_reviewer']['token'])
        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])

        Fetch = env['runbot_merge.fetch_job']
        fetches = Fetch.search([('repository', '=', repo.name), ('number', '=', prx.number)])
        assert len(fetches) == 1, f"expected one fetch for {prx.number}, found {len(fetches)}"

        env.run_crons('runbot_merge.fetch_prs_cron')
        env.run_crons()
        assert not Fetch.search([('repository', '=', repo.name), ('number', '=', prx.number)])

        c = env['runbot_merge.commit'].search([('sha', '=', prx.head)])
        assert json.loads(c.statuses) == {
            'legal/cla': {'state': 'success', 'target_url': None, 'description': None},
            'ci/runbot': {'state': 'success', 'target_url': 'http://example.org/wheee', 'description': None}
        }
        assert prx.comments == [
            seen(env, prx, users),
            (users['reviewer'], 'hansen r+'),
            (users['reviewer'], 'hansen r+'),
            seen(env, prx, users),
            (users['user'], f"@{users['user']} @{users['reviewer']} I didn't know about this PR and had to "
                            "retrieve its information, you may have to "
                            "re-approve it as I didn't see previous commands."),
        ]

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])
        assert pr.state == 'validated'

        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        assert pr.state == 'ready'

    def test_fetch_closed(self, env, repo, users, config):
        """ If an "unknown PR" is fetched while closed, it should be saved as
        closed
        """
        with repo:
            m, _ = repo.make_commits(
                None,
                Commit('initial', tree={'m': 'm'}),
                Commit('second', tree={'m2': 'm2'}),
                ref='heads/master')

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            pr = repo.make_pr(title='title', body='body', target='master', head=c1)
        env.run_crons()
        with repo:
            pr.close()

        # assume an unknown but ready PR: we don't know the PR or its head commit
        to_pr(env, pr).unlink()
        env['runbot_merge.commit'].search([('sha', '=', pr.head)]).unlink()

        # reviewer reviewers
        with repo:
            pr.post_comment('hansen r+', config['role_reviewer']['token'])

        Fetch = env['runbot_merge.fetch_job']
        fetches = Fetch.search([('repository', '=', repo.name), ('number', '=', pr.number)])
        assert len(fetches) == 1, f"expected one fetch for {pr.number}, found {len(fetches)}"

        env.run_crons('runbot_merge.fetch_prs_cron')
        env.run_crons()
        assert not Fetch.search([('repository', '=', repo.name), ('number', '=', pr.number)])

        assert to_pr(env, pr).state == 'closed'
        assert pr.comments == [
            seen(env, pr, users),
            (users['reviewer'], 'hansen r+'),
            seen(env, pr, users),
            # reviewer is set because fetch replays all the comments (thus
            # setting r+ and reviewer) but then syncs the head commit thus
            # unsetting r+ but leaving the reviewer
            (users['user'], f"@{users['user']} @{users['reviewer']} I didn't know about this PR and had to retrieve "
                            "its information, you may have to re-approve it "
                            "as I didn't see previous commands."),
        ]

    def test_rplus_unmanaged(self, env, repo, users, config):
        """ r+ on an unmanaged target should notify about
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
            repo.make_ref('heads/branch', m2)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='branch', head=c1)
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')

            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons(
            'runbot_merge.fetch_prs_cron',
            'runbot_merge.feedback_cron',
        )

        assert prx.comments == [
            (users['reviewer'], 'hansen r+'),
            (users['user'], "This PR targets the un-managed branch %s:branch, it needs to be retargeted before it can be merged." % repo.name),
            (users['user'], "Branch `branch` is not within my remit, imma just ignore it."),
        ]

    def test_rplus_review_unmanaged(self, env, repo, users, config):
        """ r+ reviews can take a different path than comments
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            m2 = repo.make_commit(m, 'second', None, tree={'m': 'm', 'm2': 'm2'})
            repo.make_ref('heads/branch', m2)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='branch', head=c1)
            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')

            prx.post_review('APPROVE', 'hansen r+', config['role_reviewer']['token'])
        env.run_crons(
            'runbot_merge.fetch_prs_cron',
            'runbot_merge.feedback_cron',
        )

        # FIXME: either split out reviews in local or merge reviews & comments in remote
        assert prx.comments[-1:] == [
            (users['user'], "I'm sorry. Branch `branch` is not within my remit."),
        ]

class TestRecognizeCommands:
    @pytest.mark.parametrize('botname', ['hansen', 'Hansen', 'HANSEN', 'HanSen', 'hAnSeN'])
    def test_botname_casing(self, repo, env, botname, config):
        """ Test that the botname is case-insensitive as people might write
        bot names capitalised or titlecased or uppercased or whatever
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'first', None, tree={'m': 'c'})
            prx = repo.make_pr(title='title', body=None, target='master', head=c)

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.state == 'opened'

        with repo:
            prx.post_comment('%s r+' % botname, config['role_reviewer']['token'])
        assert pr.state == 'approved'

    @pytest.mark.parametrize('indent', ['', '\N{SPACE}', '\N{SPACE}'*4, '\N{TAB}'])
    def test_botname_indented(self, repo, env, indent, config):
        """ matching botname should ignore leading whitespaces
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'first', None, tree={'m': 'c'})
            prx = repo.make_pr(title='title', body=None, target='master', head=c)

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.state == 'opened'

        with repo:
            prx.post_comment('%shansen r+' % indent, config['role_reviewer']['token'])
        assert pr.state == 'approved'

    def test_unknown_commands(self, repo, env, config, users):
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'first', None, tree={'m': 'c'})
            pr = repo.make_pr(title='title', body=None, target='master', head=c)
            pr.post_comment("hansen do the thing", config['role_reviewer']['token'])
            pr.post_comment('hansen @bobby-b r+ :+1:', config['role_reviewer']['token'])
        env.run_crons()

        assert pr.comments == [
            (users['reviewer'], "hansen do the thing"),
            (users['reviewer'], "hansen @bobby-b r+ :+1:"),
            seen(env, pr, users),
        ]

class TestRMinus:
    def test_rminus_approved(self, repo, env, config):
        """ approved -> r- -> opened
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'first', None, tree={'m': 'c'})
            prx = repo.make_pr(title='title', body=None, target='master', head=c)

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.state == 'opened'

        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        assert pr.state == 'approved'

        with repo:
            prx.post_comment('hansen r-', config['role_user']['token'])
        assert pr.state == 'opened'
        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        assert pr.state == 'approved'

        with repo:
            prx.post_comment('hansen r-', config['role_other']['token'])
        assert pr.state == 'approved'

        with repo:
            prx.post_comment('hansen r-', config['role_reviewer']['token'])
        assert pr.state == 'opened'

    def test_rminus_ready(self, repo, env, config):
        """ ready -> r- -> validated
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'first', None, tree={'m': 'c'})
            prx = repo.make_pr(title='title', body=None, target='master', head=c)
            repo.post_status(prx.head, 'success', 'ci/runbot')
            repo.post_status(prx.head, 'success', 'legal/cla')
        env.run_crons()

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.state == 'validated'

        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        assert pr.state == 'ready'

        with repo:
            prx.post_comment('hansen r-', config['role_user']['token'])
        assert pr.state == 'validated'
        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        assert pr.state == 'ready'

        with repo:
            prx.post_comment('hansen r-', config['role_other']['token'])
        assert pr.state == 'ready'

        with repo:
            prx.post_comment('hansen r-', config['role_reviewer']['token'])
        assert pr.state == 'validated'

    def test_rminus_staged(self, repo, env, config):
        """ staged -> r- -> validated
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'first', None, tree={'m': 'c'})
            prx = repo.make_pr(title='title', body=None, target='master', head=c)
            repo.post_status(prx.head, 'success', 'ci/runbot')
            repo.post_status(prx.head, 'success', 'legal/cla')
        env.run_crons()

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])

        # if reviewer unreviews, cancel staging & unreview
        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()
        st = pr.staging_id
        assert st

        with repo:
            prx.post_comment('hansen r-', config['role_reviewer']['token'])
        assert not st.active
        assert not pr.staging_id
        assert pr.state == 'validated'

        # if author unreviews, cancel staging & unreview
        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()
        st = pr.staging_id
        assert st

        with repo:
            prx.post_comment('hansen r-', config['role_user']['token'])
        assert not st.active
        assert not pr.staging_id
        assert pr.state == 'validated'

        # if rando unreviews, ignore
        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()
        st = pr.staging_id
        assert st

        with repo:
            prx.post_comment('hansen r-', config['role_other']['token'])
        assert pr.staging_id == st
        assert pr.state == 'ready'

    def test_split(self, env, repo, config):
        """ Should remove the PR from its split, and possibly delete the split
        entirely.
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'first', None, tree={'m': 'm', '1': '1'})
            repo.make_ref('heads/p1', c)
            prx1 = repo.make_pr(title='t1', body='b1', target='master', head='p1')
            repo.post_status(prx1.head, 'success', 'legal/cla')
            repo.post_status(prx1.head, 'success', 'ci/runbot')
            prx1.post_comment('hansen r+', config['role_reviewer']['token'])

            c = repo.make_commit(m, 'first', None, tree={'m': 'm', '2': '2'})
            repo.make_ref('heads/p2', c)
            prx2 = repo.make_pr(title='t2', body='b2', target='master', head='p2')
            repo.post_status(prx2.head, 'success', 'legal/cla')
            repo.post_status(prx2.head, 'success', 'ci/runbot')
            prx2.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        pr1, pr2 = env['runbot_merge.pull_requests'].search([], order='number')
        assert pr1.number == prx1.number
        assert pr2.number == prx2.number
        assert pr1.staging_id == pr2.staging_id
        s0 = pr1.staging_id

        with repo:
            repo.post_status('heads/staging.master', 'failure', 'ci/runbot')
        env.run_crons()

        assert pr1.staging_id and pr1.staging_id != s0, "pr1 should have been re-staged"
        assert not pr2.staging_id, "pr2 should not"
        # TODO: remote doesn't currently handle env context so can't mess
        #       around using active_test=False
        assert env['runbot_merge.split'].search([])

        with repo:
            # prx2 was actually a terrible idea!
            prx2.post_comment('hansen r-', config['role_reviewer']['token'])
        # probably not necessary ATM but...
        env.run_crons()

        assert pr2.state == 'validated', "state should have been reset"
        assert not env['runbot_merge.split'].search([]), "there should be no split left"

    def test_rminus_p0(self, env, repo, config, users):
        """ In and of itself r- doesn't do anything on p=0 since they bypass
        approval, so unstage and downgrade to p=1.
        """

        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c = repo.make_commit(m, 'first', None, tree={'m': 'c'})
            prx = repo.make_pr(title='title', body=None, target='master', head=c)
            repo.post_status(prx.head, 'success', 'ci/runbot')
            repo.post_status(prx.head, 'success', 'legal/cla')
            prx.post_comment('hansen p=0', config['role_reviewer']['token'])
        env.run_crons()

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number),
        ])
        assert pr.priority == 0
        assert pr.staging_id

        with repo:
            prx.post_comment('hansen r-', config['role_reviewer']['token'])
        env.run_crons()
        assert not pr.staging_id, "pr should have been unstaged"
        assert pr.priority == 1, "priority should have been downgraded"
        assert prx.comments == [
            (users['reviewer'], 'hansen p=0'),
            seen(env, prx, users),
            (users['reviewer'], 'hansen r-'),
            (users['user'], "PR priority reset to 1, as pull requests with priority 0 ignore review state."),
        ]

class TestComments:
    def test_address_method(self, repo, env, config):
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)

            repo.post_status(prx.head, 'success', 'legal/cla')
            repo.post_status(prx.head, 'success', 'ci/runbot')
            prx.post_comment('hansen delegate=foo', config['role_reviewer']['token'])
            prx.post_comment('@hansen delegate=bar', config['role_reviewer']['token'])
            prx.post_comment('#hansen delegate=baz', config['role_reviewer']['token'])

        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])

        assert {p.github_login for p in pr.delegates} \
            == {'foo', 'bar', 'baz'}

    def test_delete(self, repo, env, config):
        """ Comments being deleted should be ignored
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])

        with repo:
            cid = prx.post_comment('hansen r+', config['role_reviewer']['token'])
            # unreview by pushing a new commit
            repo.update_ref(prx.ref, repo.make_commit(c1, 'second', None, tree={'m': 'c2'}), force=True)
        assert pr.state == 'opened'
        with repo:
            prx.delete_comment(cid, config['role_reviewer']['token'])
        # check that PR is still unreviewed
        assert pr.state == 'opened'

    def test_edit(self, repo, env, config):
        """ Comments being edited should be ignored
        """
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])

        with repo:
            cid = prx.post_comment('hansen r+', config['role_reviewer']['token'])
            # unreview by pushing a new commit
            repo.update_ref(prx.ref, repo.make_commit(c1, 'second', None, tree={'m': 'c2'}), force=True)
        assert pr.state == 'opened'
        with repo:
            prx.edit_comment(cid, 'hansen r+ edited', config['role_reviewer']['token'])
        # check that PR is still unreviewed
        assert pr.state == 'opened'

class TestFeedback:
    def test_ci_approved(self, repo, env, users, config):
        """CI failing on an r+'d PR sends feedback"""
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])

        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        assert pr.state == 'approved'

        with repo:
            repo.post_status(prx.head, 'failure', 'ci/runbot')
        env.run_crons()

        assert prx.comments == [
            (users['reviewer'], 'hansen r+'),
            seen(env, prx, users),
            (users['user'], "@%(user)s @%(reviewer)s 'ci/runbot' failed on this reviewed PR." % users)
        ]

    def test_review_failed(self, repo, env, users, config):
        """r+-ing a PR with failed CI sends feedback"""
        with repo:
            m = repo.make_commit(None, 'initial', None, tree={'m': 'm'})
            repo.make_ref('heads/master', m)

            c1 = repo.make_commit(m, 'first', None, tree={'m': 'c1'})
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
        pr = env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', repo.name),
            ('number', '=', prx.number)
        ])

        with repo:
            repo.post_status(prx.head, 'failure', 'ci/runbot')
        env.run_crons()
        assert pr.state == 'opened'

        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        assert pr.state == 'approved'

        env.run_crons()

        assert prx.comments == [
            seen(env, prx, users),
            (users['reviewer'], 'hansen r+'),
            (users['user'], "@%s you may want to rebuild or fix this PR as it has failed CI." % users['reviewer'])
        ]

class TestInfrastructure:
    @pytest.mark.skip(reason="Don't want to implement")
    def test_protection(self, repo):
        """ force-pushing on a protected ref should fail
        """
        with repo:
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

class TestEmailFormatting:
    def test_simple(self, env):
        p1 = env['res.partner'].create({
            'name': 'Bob',
            'email': 'bob@example.com',
        })
        assert p1.formatted_email == 'Bob <bob@example.com>'

    def test_noemail(self, env):
        p1 = env['res.partner'].create({
            'name': 'Shultz',
            'github_login': 'Osmose99',
        })
        assert p1.formatted_email == 'Shultz <Osmose99@users.noreply.github.com>'
