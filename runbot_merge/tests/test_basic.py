import contextlib
import datetime
import itertools
import json
import re
import shutil
import textwrap
import time
from collections.abc import Iterator
from operator import itemgetter
from typing import Callable, Literal
from unittest import mock
from urllib.parse import quote

import pytest
import requests
from lxml import html

import odoo
from utils import _simple_init, seen, matches, get_partner, Commit, pr_page, to_pr, part_of, ensure_one, read_tracking_value, node, log_to_node


@pytest.fixture(autouse=True, params=["statuses", "runbot"])
def status_mode(request, env, project, repo, port, RepoType) -> Iterator[Literal["statuses", "rpc", "runbot"]]:
    """Hook in support for validation alternatives:

    - github statuses (default)
    - runbot hook

    The runbot hook is the only one which also affects the way PRs are validated,
    as a result it can not persist statuses on PRs when a HEAD changes.
    """
    # apparently side_effect + wraps on unbound method don't work correctly,
    # the wrapped method does get called when returning DEFAULT but *the
    # instance (subject) is not sent along for the ride* so the call fails.
    post_status = RepoType.post_status
    match request.param:
        case "statuses":
            project.write({"staging_statuses": True})
            cm = contextlib.nullcontext()

        case "runbot":
            project.write({"staging_statuses": False})
            def _post_status(repo, ref, status, context='default', **kw):
                if ref.startswith(('staging.', 'heads/staging.')):
                    branchname = ref.removeprefix('heads/').removeprefix('staging.')
                    st = env['runbot_merge.stagings'].search([('target.name', '=', branchname)])
                    oid = st.id
                else:
                    if re.fullmatch(r'[a-z0-9]{40}', ref, flags=re.IGNORECASE):
                        cond = ('head', '=', ref)
                    else:
                        cond = ('label', '=like', '%:' + ref.removeprefix('heads/'))
                    for i in range(5):
                        pr = env['runbot_merge.pull_requests'].search([
                            ('repository.name', '=', repo.name),
                            cond
                        ])
                        if pr:
                            oid = quote(pr.display_name.replace('#', '/'))
                            break
                        time.sleep(i)
                    else:
                        raise TimeoutError(f"could not find PR {cond }")

                sha = repo.commit(ref).id
                r = requests.post(
                    f"http://localhost:{port}/runbot_merge/{oid}/statuses",
                    data={'sha': sha, 'context': context, 'status': status, **kw},
                )
                assert r.ok, r.reason
            cm = mock.patch.object(RepoType, "post_status", _post_status)
        case mode:
            raise ValueError(f"unknown staging mode {mode}")

    with cm:
        yield request.param


def test_trivial_flow(env, repo, page, users, config, project, partners, status_mode):
    project.repo_ids.required_statuses = 'legal/cla,ci/runbot'
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

        [c2] = repo.make_commits(
            'other',
            Commit('forgot a bit', tree={'whee': 'kjfdsh'}),
            ref='heads/other',
            make=False,
        )

    pr_id = to_pr(env, pr)
    assert pr_id.state == 'opened'
    env.run_crons()
    assert pr.comments == [seen(env, pr, users)]

    pr_dashboard = pr_page(page, pr)
    s = pr_dashboard.cssselect('.alert-info > ul > li')
    assert [it.get('class') for it in s] == ['fail', 'fail', ''],\
        "merge method unset, review missing, no CI"
    assert dict(zip(
        (e.text_content() for e in pr_dashboard.cssselect('dl.runbot-merge-fields dt')),
        (e.text_content() for e in pr_dashboard.cssselect('dl.runbot-merge-fields dd')),
    )) == {
        'label': f"{config['github']['owner']}:other",
        'head': c2,
    }

    with repo:
        repo.post_status(c2, 'success', 'legal/cla')
        repo.post_status(c2, 'success', 'ci/runbot')
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
        repo.post_status('staging.master', 'success', 'ci/runbot', target_url='http://foo.com/pog')
        repo.post_status('staging.master', 'success', 'legal/cla')
        # the should not block the merge because it's not part of the requirements
        repo.post_status('staging.master', 'failure', 'ci/lint', target_url='http://ignored.com/whocares')
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
    s = p.cssselect('.staging div.staging-statuses a')
    assert len(s) == 2, "not logged so only *required* statuses"
    for e, status in zip(s, ['legal/cla', 'ci/runbot']):
        assert set(e.classes) == {'bg-success'}
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
        'whee': 'kjfdsh',
    }
    assert master.message == "gibberish\n\nblahblah\n\ncloses {repo.name}#1"\
                             "\n\nSigned-off-by: {reviewer.formatted_email}"\
                             .format(repo=repo, reviewer=get_partner(env, users['reviewer']))

    # reverse because the messages are in newest-to-oldest by default
    # (as that's how you want to read them)
    messages = pr_id.message_ids[::-1].mapped(lambda m: (
        m.author_id.display_name,
        m.body,
        list(map(read_tracking_value, m.tracking_value_ids)),
    ))

    if status_mode == 'runbot':
        statuses_msg = '<p>statuses updated by runbot</p>'
    else:
        statuses_msg = f'<p>statuses changed on {c2}</p>'
    assert list(messages) == [
        (users['user'], '<p>Pull Request created</p>', []),
        (users['user'], '', [('head', c1, c2)]),
        ('OdooBot', statuses_msg, [('state', 'Opened', 'Validated')]),
        # reviewer approved changing the state and setting reviewer as reviewer
        # plus set merge method
        (partners['reviewer'].name, '', [
            ('merge_method', '', 'rebase and merge, using the PR as merge commit message'),
            ('reviewed_by', '', partners['reviewer'].name),
            ('state', 'Validated', 'Ready'),
        ]),
        # staging succeeded
        (matches('$$'), f'<p>staging {st.id} succeeded</p>', [
            # set merge date
            ('merge_date', False, pr_id.merge_date),
            # updated state
            ('state', 'Ready', 'Merged'),
        ]),
    ]

class TestCommitMessage:
    def test_commit_simple(self, env, repo, users, config):
        """ verify 'closes ...' is correctly added in the commit message
        """
        with repo:
            [c1] = repo.make_commits(None, Commit('first!', tree={'f': 'm1'}), ref='heads/master')
            [c2] = repo.make_commits(c1, Commit('simple commit message', tree={'f': 'm2'}))

            prx = repo.make_pr(title='title', body='body', target='master', head=c2)
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('staging.master', 'success')
        env.run_crons()

        master = repo.commit('heads/master')
        assert master.message == "simple commit message\n\ncloses {repo.name}#1"\
                                 "\n\nSigned-off-by: {reviewer.formatted_email}"\
                                 .format(repo=repo, reviewer=get_partner(env, users['reviewer']))

    def test_commit_existing(self, env, repo, users, config):
        """ verify do not duplicate 'closes' instruction
        """
        with repo:
            [c1] = repo.make_commits(None, Commit('first!', tree={'f': 'm1'}), ref='heads/master')
            [c2] = repo.make_commits(c1, Commit('simple commit message that closes #1', tree={'f': 'm2'}))

            prx = repo.make_pr(title='title', body='body', target='master', head=c2)
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('staging.master', 'success')
        env.run_crons()

        master = repo.commit('heads/master')
        # closes #1 is already present, should not modify message
        assert master.message == "simple commit message that closes #1"\
                                 "\n\nSigned-off-by: {reviewer.formatted_email}"\
                                 .format(reviewer=get_partner(env, users['reviewer']))

    def test_commit_other(self, env, repo, users, config):
        """ verify do not duplicate 'closes' instruction
        """
        with repo:
            [c1] = repo.make_commits(None, Commit('first!', tree={'f': 'm1'}), ref='heads/master')
            [c2] = repo.make_commits(c1, Commit('simple commit message that closes odoo/enterprise#1', tree={'f': 'm2'}))

            prx = repo.make_pr(title='title', body='body', target='master', head=c2)
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('staging.master', 'success')
        env.run_crons()

        master = repo.commit('heads/master')
        # closes on another repositoy, should modify the commit message
        assert master.message == "simple commit message that closes odoo/enterprise#1\n\ncloses {repo.name}#1"\
                                 "\n\nSigned-off-by: {reviewer.formatted_email}"\
                                 .format(repo=repo, reviewer=get_partner(env, users['reviewer']))

    def test_commit_wrong_number(self, env, repo, users, config):
        """ verify do not match on a wrong number
        """
        with repo:
            [c1] = repo.make_commits(None, Commit('first!', tree={'f': 'm1'}), ref='heads/master')
            [c2] = repo.make_commits(c1, Commit('simple commit message that closes #11', tree={'f': 'm2'}))

            prx = repo.make_pr(title='title', body='body', target='master', head=c2)
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('staging.master', 'success')
        env.run_crons()

        master = repo.commit('heads/master')
        # closes on another repositoy, should modify the commit message
        assert master.message == "simple commit message that closes #11\n\ncloses {repo.name}#1"\
                                 "\n\nSigned-off-by: {reviewer.formatted_email}"\
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
            [c1] = repo.make_commits(None, Commit('first!', tree={'f': 'm1'}), ref='heads/master')
            [c2] = repo.make_commits(c1, Commit('simple commit message', tree={'f': 'm2'}))

            prx = repo.make_pr(title='title', body='body', target='master', head=c2)
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen delegate=%s' % users['other'], config["role_reviewer"]["token"])
            prx.post_comment('hansen r+', config['role_other']['token'])
        env.run_crons()

        with repo:
            repo.post_status('staging.master', 'success')
        env.run_crons()

        master = repo.commit('heads/master')
        assert master.message == "simple commit message\n\ncloses {repo.name}#1"\
                                 "\n\nSigned-off-by: {reviewer.formatted_email}"\
                                 .format(repo=repo, reviewer=get_partner(env, users['other']))

    def test_commit_coauthored(self, env, repo, users, config):
        """ verify 'closes ...' and 'Signed-off-by' are added before co-authored-by tags.

        Also checks that all co-authored-by are moved at the end of the
        message
        """
        with repo:
            [c1] = repo.make_commits(None, Commit('first!', tree={'f': 'm1'}), ref='heads/master')
            [c2] = repo.make_commits(c1, Commit('''simple commit message


Co-authored-by: Bob <bob@example.com>

Fixes a thing''', tree={'f': 'm2'}))

            prx = repo.make_pr(title='title', body='body', target='master', head=c2)
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('staging.master', 'success')
        env.run_crons()

        master = repo.commit('heads/master')
        assert master.message == """simple commit message

Fixes a thing

closes {repo.name}#1

Signed-off-by: {reviewer.formatted_email}
Co-authored-by: Bob <bob@example.com>""".format(
            repo=repo,
            reviewer=get_partner(env, users['reviewer'])
        )

class TestWebhookSecurity:
    @pytest.fixture(autouse=True)
    def add_secret_to_source(self, env, repo):
        env['runbot_merge.events_sources'].search([
            ('repository', '=', repo.name),
        ]).secret = "a secret"

    def test_no_secret(self, env, project, repo):
        """ Test 1: didn't add a secret to the repo, should be ignored
        """
        with repo:
            [m] = repo.make_commits(None, Commit("initial", tree={'a': 'some content'}), ref='heads/master')

            [c0] = repo.make_commits(m, Commit('replace file contents', tree={'a': 'some other content'}))
            pr0 = repo.make_pr(title="gibberish", body="blahblah", target='master', head=c0)

        with pytest.raises(TimeoutError):
            to_pr(env, pr0)

    def test_wrong_secret(self, env, project, repo):
        with repo:
            repo.set_secret("wrong secret")

            [m] = repo.make_commits(None, Commit("initial", tree={'a': 'some content'}), ref='heads/master')

            [c0] = repo.make_commits(m, Commit('replace file contents', tree={'a': 'some other content'}))
            pr0 = repo.make_pr(title="gibberish", body="blahblah", target='master', head=c0)

        with pytest.raises(TimeoutError):
            to_pr(env, pr0)

    def test_correct_secret(self, env, project, repo):
        with repo:
            repo.set_secret("a secret")

            [m] = repo.make_commits(None, Commit("initial", tree={'a': 'some content'}), ref='heads/master')

            [c0] = repo.make_commits(m, Commit('replace file contents', tree={'a': 'some other content'}))
            pr0 = repo.make_pr(title="gibberish", body="blahblah", target='master', head=c0)

        assert to_pr(env, pr0)

def test_staging_ongoing(env, repo, config):
    with repo:
        # create base branch
        [m] = repo.make_commits(None, Commit('initial', tree={'a': 'some content'}), ref='heads/master')

        # create PR
        _, c1 = repo.make_commits(
            m,
            Commit('replace file contents', tree={'a': 'some other content'}),
            Commit('add file', tree={'b': 'a second file'}),
        )
        pr1 = repo.make_pr(title="gibberish", body="blahblah", target='master', head=c1)
        repo.post_status(c1, 'success')
        pr1.post_comment("hansen r+ rebase-merge", config['role_reviewer']['token'])
    env.run_crons()
    pr1 = to_pr(env, pr1)
    assert pr1.staging_id

    with repo:
        # create second PR and make ready for staging
        _, c3 = repo.make_commits(
            m,
            Commit('other', tree={'a': 'some content', 'c': 'ccc'}),
            Commit('other', tree={'d': 'ddd'}),
        )
        pr2 = repo.make_pr(title='gibberish', body='blahblah', target='master', head=c3)
        repo.post_status(c3, 'success')
        pr2.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()
    p_2 = to_pr(env, pr2)
    assert p_2.state == 'ready', "PR2 should not have been staged since there is a pending staging for master"

    with repo:
        repo.post_status('staging.master', 'success')
    env.run_crons()
    assert pr1.state == 'merged'
    assert p_2.staging_id

    with repo:
        repo.post_status('staging.master', 'success')
    env.run_crons()
    assert p_2.state == 'merged'

def test_staging_concurrent(env, repo, config):
    """ test staging to different targets, should be picked up together """
    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/1.0')
        repo.make_ref('heads/2.0', m)

    env['runbot_merge.project'].search([]).write({
        'branch_ids': [(0, 0, {'name': '1.0'}), (0, 0, {'name': '2.0'})],
    })

    with repo:
        _, c11 = repo.make_commits(
            m,
            Commit('AAA', tree={'m': 'm', 'a': 'a'}),
            Commit('BBB', tree={'b': 'b'}),
        )
        pr1 = repo.make_pr(title='t1', body='b1', target='1.0', head=c11)
        repo.post_status(pr1.head, 'success')
        pr1.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])

        _, c21 = repo.make_commits(
            m,
            Commit('CCC', tree={'c': 'c'}),
            Commit('DDD', tree={'d': 'd'}),
        )
        pr2 = repo.make_pr(title='t2', body='b2', target='2.0', head=c21)
        repo.post_status(pr2.head, 'success')
        pr2.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()

    pr1 = to_pr(env, pr1)
    assert pr1.staging_id
    pr2 = to_pr(env, pr2)
    assert pr2.staging_id


def test_staging_conflict_first(env, repo, users, config, page):
    """ If the first batch of a staging triggers a conflict, the PR should be
    marked as in error
    """
    with repo:
        m1, _ = repo.make_commits(
            None,
            Commit('initial', tree={'f': 'm1'}),
            Commit('second', tree={'f': 'm2'}),
            ref='heads/master',
        )

        _, c2 = repo.make_commits(
            m1,
            Commit('other second', tree={'f': 'c1'}),
            Commit('third', tree={'f': 'c2'}),
        )
        pr = repo.make_pr(title='title', body='body', target='master', head=c2)
        repo.post_status(pr.head, 'success')
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


@pytest.mark.skipif(not shutil.which('mergiraf'), reason='mergiraf not installed')
def test_staging_textual_only_conflict(env, project, repo, users, config):
    """If mergiraf is enabled and the conflict can be resolved by doing a
    structural merge, then the staging should succeed
    """
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
            ref='heads/master',
        )

        [c2] = repo.make_commits(
            m1,
            Commit('other second', tree={'f.py': '''\
def f():
    b = 0
    return b
'''}),
        )
        pr = repo.make_pr(title='title', body='body', target='master', head=c2)
        repo.post_status(pr.head, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    pr_id = to_pr(env, pr)
    assert pr_id.state == 'ready'
    assert pr_id.staging_id, "the PR should have been staged"
    assert repo.read_tree(repo.commit('staging.master')) == {
        'f.py': '''\
def f():
    b = 1
    return b
'''
    }

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
        repo.post_status(pr0.head, 'success')
        pr0.post_comment('hansen r+', config['role_reviewer']['token'])

    with repo:
        repo.make_commits(m, Commit('second pr', tree={'a': '3'}), ref='heads/pr1')
        pr1 = repo.make_pr(target='master', head='pr1')
        repo.post_status(pr1.head, 'success')
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
        repo.post_status('staging.master', 'success')
    env.run_crons()

    assert pr1_id.state == 'error', "now pr1 should be in error"


@pytest.mark.parametrize('update_op', [
    pytest.param(
        lambda _: {'timeout_limit': datetime.datetime.now().isoformat(" ", "seconds")},
        id="set-timeout-limit",
    ),
    pytest.param(
        lambda timeout: {'staged_at': (datetime.datetime.now() - datetime.timedelta(minutes=2*timeout)).isoformat(" ", "seconds")},
        id="set-staged-at",
    ),
])
def test_staging_ci_timeout(env, repo, config, page, update_op: Callable[[int], dict]):
    """If a staging timeouts (~ delay since staged greater than
    configured)... requeue?
    """
    with repo:
        m, _, c2 = repo.make_commits(
            None,
            Commit('initial', tree={'f': 'm'}),
            Commit('first', tree={'f': 'c1'}),
            Commit('second', tree={'f': 'c2'}),
        )
        repo.make_ref('heads/master', m)

        pr = repo.make_pr(title='title', body='body', target='master', head=c2)
        repo.post_status(pr.head, 'success')
        pr.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()

    pr_id = to_pr(env, pr)
    assert pr_id.staging_id
    timeout = env['runbot_merge.project'].search([]).ci_timeout

    pr_id.staging_id.write(update_op(timeout))
    env.run_crons(None)
    assert pr_id.state == 'error', "timeout should fail the PR"

    dangerbox = pr_page(page, pr).cssselect('.alert-danger span')
    assert dangerbox
    assert dangerbox[0].text == 'timed out (>60 minutes)'

def test_timeout_bump_on_pending(env, repo, config, project):
    project.repo_ids.required_statuses = 'legal/cla,ci/runbot'
    with repo:
        m, c = repo.make_commits(
            None,
            Commit('initial', tree={'f': '0'}),
            Commit('c', tree={'f': '1'}),
        )
        repo.make_ref('heads/master', m)

        prx = repo.make_pr(target='master', head=c)
        repo.post_status(prx.head, 'success', 'ci/runbot')
        repo.post_status(prx.head, 'success', 'legal/cla')
        prx.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    st = env['runbot_merge.stagings'].search([])
    old_timeout = odoo.fields.Datetime.to_string(datetime.datetime.now() - datetime.timedelta(days=15))
    st.timeout_limit = old_timeout
    with repo:
        repo.post_status('staging.master', 'pending', 'ci/runbot')
    env.run_crons(None)
    assert st.timeout_limit > old_timeout, "receiving a pending status should bump the timeout"

    st.timeout_limit = old_timeout
    # clear the statuses cache to remove the memoized status times
    st.statuses_cache = "{}"
    st.head_ids.statuses = "{}"
    with repo:
        repo.post_status('staging.master', 'success', 'legal/cla')
    env.run_crons(None)
    assert st.timeout_limit == old_timeout, "receiving a success status should *not* bump the timeout"

def test_staging_ci_failure_single(env, repo, users, config, page):
    """ on failure of single-PR staging, mark & notify failure
    """
    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

        _, c2 = repo.make_commits(
            m,
            Commit('first', tree={'m': 'c1'}),
            Commit('second', tree={'m': 'c2'}),
        )
        pr = repo.make_pr(title='title', body='body', target='master', head=c2)
        repo.post_status(pr.head, 'success')
        pr.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()
    pr_id = to_pr(env, pr)
    assert pr_id.staging_id

    with repo:
        repo.post_status('staging.master', 'failure', 'a/b')
        repo.post_status('staging.master', 'failure') # stable genius
    env.run_crons()
    assert pr_id.state == 'error'

    assert pr.comments == [
        (users['reviewer'], 'hansen r+ rebase-merge'),
        seen(env, pr, users),
        (users['user'], "Merge method set to rebase and merge, using the PR as merge commit message."),
        (users['user'], '@%(user)s @%(reviewer)s staging failed: default' % users)
    ]

    dangerbox = pr_page(page, pr).cssselect('.alert-danger span')
    assert dangerbox
    assert dangerbox[0].text == 'default'


def test_ff_failure(env, repo, config, page):
    """ target updated while the PR is being staged => redo staging """
    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

        _, c2 = repo.make_commits(
            m,
            Commit('first', tree={'m': 'c1'}),
            Commit('second', tree={'m': 'c2'}),
        )
        prx = repo.make_pr(title='title', body='body', target='master', head=c2)
        repo.post_status(prx.head, 'success')
        prx.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()
    st = to_pr(env, prx).staging_id
    assert st

    with repo:
        [m2] = repo.make_commits('heads/master', Commit('cockblock', tree={'m': 'm', 'm2': 'm2'}), ref='heads/master', make=False)
    assert repo.commit('heads/master').id == m2

    # report staging success & run cron to merge
    staging = repo.commit('staging.master')
    with repo:
        repo.post_status('staging.master', 'success')
    env.run_crons()

    assert st.reason == 'rejected (non-fast-forward)'
    # check that it's added as title on the staging
    doc = html.fromstring(page('/runbot_merge'))
    _new, prev = doc.cssselect('li.staging')

    assert 'bg-gray-lighter' in prev.classes, "ff failure is ~ cancelling"
    assert 'fast forward failed (rejected (non-fast-forward))' in prev.get('title')

    assert to_pr(env, prx).staging_id, "merge should not have succeeded"
    assert repo.commit('staging.master').id != staging.id,\
        "PR should be staged to a new commit"


def test_ff_failure_batch(env, repo, users, config):
    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

        repo.make_commits(
            m,
            Commit('a1', tree={'m': 'm', 'a': '1'}),
            Commit('a2', tree={'m': 'm', 'a': '2'}),
            ref='heads/A',
        )
        A = repo.make_pr(title='A', body=None, target='master', head='A')
        repo.post_status(A.head, 'success')
        A.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])

        repo.make_commits(
            m,
            Commit('b1', tree={'m': 'm', 'b': '1'}),
            Commit('b2', tree={'m': 'm', 'b': '2'}),
            ref='heads/B',
        )
        B = repo.make_pr(title='B', body=None, target='master', head='B')
        repo.post_status(B.head, 'success')
        B.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])

        repo.make_commits(
            m,
            Commit('c1', tree={'m': 'm', 'c': '1'}),
            Commit('c2', tree={'m': 'm', 'c': '2'}),
            ref='heads/C',
        )
        C = repo.make_pr(title='C', body=None, target='master', head='C')
        repo.post_status(C.head, 'success')
        C.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()

    pr_a = to_pr(env, A)
    pr_b = to_pr(env, B)
    pr_c = to_pr(env, C)

    messages = [
        c['commit']['message']
        for c in repo.log('staging.master')
    ]
    assert part_of('a2', pr_a) in messages
    assert part_of('b2', pr_b) in messages
    assert part_of('c2', pr_c) in messages

    # block FF
    with repo:
        repo.make_commits('heads/master', Commit('NO!', tree={'m': 'm2'}), ref='heads/master', make=False)

    old_staging = repo.commit('staging.master')
    # confirm staging
    with repo:
        repo.post_status('staging.master', 'success')
    env.run_crons()
    new_staging = repo.commit('staging.master')

    assert new_staging.id != old_staging.id

    # confirm again
    with repo:
        repo.post_status('staging.master', 'success')
    env.run_crons()
    messages = {
        c['commit']['message']
        for c in repo.log('heads/master')
    }
    reviewer = get_partner(env, users["reviewer"]).formatted_email
    assert messages == {
        'initial', 'NO!',
        part_of('a1', pr_a), part_of('a2', pr_a), f'A\n\ncloses {pr_a.display_name}\n\nSigned-off-by: {reviewer}',
        part_of('b1', pr_b), part_of('b2', pr_b), f'B\n\ncloses {pr_b.display_name}\n\nSigned-off-by: {reviewer}',
        part_of('c1', pr_c), part_of('c2', pr_c), f'C\n\ncloses {pr_c.display_name}\n\nSigned-off-by: {reviewer}',
    }

class TestPREdition:
    def test_edit(self, env, project, repo, config):
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
            [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')
            repo.make_ref('heads/1.0', m)
            repo.make_ref('heads/2.0', m)

            _, c2 = repo.make_commits(
                m,
                Commit('first', tree={'m': 'c1'}),
                Commit('second', tree={'m': 'c2'}),
            )
            prx = repo.make_pr(title='title', body='body', target='master', head=c2)
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen rebase-ff r+', config['role_reviewer']['token'])
        env.run_crons()

        pr = to_pr(env, prx)
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
        assert st.state == 'cancelled', f"expected cancellation, got {st.state}"
        assert st.reason == f"{pr.display_name} target (base) branch was changed from '{project.name}:master' to '{project.name}:1.0'"

        with repo: prx.base = '2.0'
        assert not pr.exists()
        env.run_crons()

        with repo: prx.base = '1.0'
        assert to_pr(env, prx).target == branch_1

    def test_retarget_update_commits(self, env, project, repo):
        """ Retargeting a PR should update its commits count, as well as follow
        the new target's requirements
        """
        project.repo_ids.write({
            'status_ids': [
                (5, 0, 0),
                (0, 0, {'context': 'a', 'branch_filter': [('name', '=', 'master')]}),
                (0, 0, {'context': 'b', 'branch_filter': [('name', '!=', 'master')]}),
            ]
        })
        branch_1 = env['runbot_merge.branch'].create({
            'name': '1.0',
            'project_id': env['runbot_merge.project'].search([]).id,
        })
        master = env['runbot_merge.branch'].search([('name', '=', 'master')])

        with repo:
            # master is 1 commit ahead of 1.0
            [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/1.0')
            [m2] = repo.make_commits(m, Commit('second', tree={'m': 'm2'}), ref='heads/master')

            # the PR builds on master, but is errorneously targeted to 1.0
            repo.make_commits(m2, Commit('first', tree={'m': 'm3'}), ref='heads/abranch')
            prx = repo.make_pr(title='title', body='body', target='1.0', head='abranch')
            repo.post_status('heads/abranch', 'success', 'a')
        env.run_crons()
        pr = to_pr(env, prx)
        assert not pr.squash
        assert pr.status == 'pending'
        assert pr.state == 'opened'

        with repo:
            prx.base = 'master'
        assert pr.target == master
        assert pr.squash
        assert pr.status == 'success'
        assert pr.state == 'validated'

        with repo:
            prx.base = '1.0'
        assert pr.target == branch_1
        assert not pr.squash
        assert pr.status == 'pending'
        assert pr.state == 'opened'

        # check if things also work right when modifying the PR then
        # retargeting (don't see why not but...)
        with repo:
            [c2] = repo.make_commits(m2, Commit('xxx', tree={'m': 'm4'}), ref=prx.ref, make=False)
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
        pr = to_pr(env, prx)
        assert pr.target == branch_1
        with repo:
            prx.close()
        with repo:
            prx.base = 'master'

def test_close_staged(env, repo, config, page, request):
    """
    When closing a staged PR, cancel the staging
    """
    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

        [c] = repo.make_commits(m, Commit('fist', tree={'m': 'c1'}))
        prx = repo.make_pr(title='title', body='body', target='master', head=c)
        repo.post_status(prx.head, 'success')
        prx.post_comment('hansen r+', config['role_reviewer']['token'])
    pr = to_pr(env, prx)
    env.run_crons()
    assert pr.reviewed_by
    assert pr.state == 'ready'
    assert pr.staging_id

    with repo:
        prx.close()
    env.run_crons()

    assert not pr.staging_id
    assert not env['runbot_merge.stagings'].search([])
    assert pr.state == 'closed'
    assert pr_page(page, prx).cssselect('.alert-light')
    assert not pr.reviewed_by

    with repo:
        prx.open()
    assert pr.state == 'validated'
    assert not pr.reviewed_by

    with repo:
        prx.post_comment('hansen r+', config['role_reviewer']['token'])
    assert pr.reviewed_by
    pr.write({'closed': True})
    assert not pr.reviewed_by

def test_forward_port(env, repo, config):
    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

        *_, head = repo.make_commits(
            m,
            *(Commit('c_%03d' % i, tree={'m': 'm', 'f': str(i)}) for i in range(110))
        )
    # not sure why we wanted to wait here

    with repo:
        pr = repo.make_pr(title='PR', body=None, target='master', head=head)
        repo.post_status(pr.head, 'success')
        pr.post_comment('hansen r+ merge', config['role_reviewer']['token'])
    env.run_crons()

    st = repo.commit('staging.master')

    with repo:
        repo.post_status('staging.master', 'success')
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
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

        repo.make_commits(m, Commit('A', tree={'m': 'm', 'a': 'a'}), ref='heads/a')
        pr_a = repo.make_pr(title='A', body=None, target='master', head='a')
        repo.post_status(pr_a.head, 'success')
        pr_a.post_comment('hansen r+', config['role_reviewer']['token'])

        repo.make_commits(m, Commit('B', tree={'m': 'm', 'b': 'b'}), ref='heads/b')
        pr_b = repo.make_pr(title='B', body=None, target='master', head='b')
        repo.post_status(pr_b.head, 'success')
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

    env.run_crons(None)

    assert pr_a.comments == [
        (users['reviewer'], 'hansen r+'),
        seen(env, pr_a, users),
        (users['user'], matches('Unable to stage PR')),
    ]
    assert pr_b.comments == [
        (users['reviewer'], 'hansen r+'),
        seen(env, pr_b, users),
    ]
    assert repo.read_tree(repo.commit('staging.master')) == {
        'm': 'm',
        'b': 'b',
    }

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
        repo.post_status(c, 'success')
        prx.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with repo:
        repo.post_status('staging.master', 'success')
    env.run_crons()
    pr = to_pr(env, prx)
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
            m, c = repo.make_commits(
                None,
                Commit('initial', tree={'0': '0'}),
                Commit('first', tree={'0': '1'}),
            )
            repo.make_ref('heads/master', m)

            pr = repo.make_pr(title='title', body='body', target='master', head=c)
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        pr_id = to_pr(env, pr)

        st = env['runbot_merge.stagings'].search([], context={'active_test': False})
        assert st.state == 'success'
        assert pr_id.state == 'merged'

    def test_updated(self, env, repo, config):
        env['runbot_merge.repository'].search([('name', '=', repo.name)]).status_ids = False
        with repo:
            m, c = repo.make_commits(
                None,
                Commit('initial', tree={'0': '0'}),
                Commit('first', tree={'0': '1'}),
            )
            repo.make_ref('heads/master', m)

            pr = repo.make_pr(title='title', body='body', target='master', head=c)
        env.run_crons()

        pr_id = to_pr(env, pr)
        assert pr_id.state == 'validated'

        # normal push
        with repo:
            repo.make_commits(c, repo.Commit('second', tree={'0': '2'}), ref=pr.ref)
        env.run_crons()
        assert pr_id.state == 'validated'
        with repo:
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
        assert pr_id.state == 'ready'

        # force push
        with repo:
            repo.make_commits(m, repo.Commit('xxx', tree={'0': 'm'}), ref=pr.ref)
        env.run_crons()
        assert pr_id.state == 'validated'
        with repo:
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
        assert pr_id.state == 'ready'

class TestRetry:
    @pytest.mark.parametrize('retrier', ['user', 'other', 'reviewer'])
    def test_retry_comment(self, env, repo, retrier, users, config):
        """ An accepted but failed PR should be re-tried when the author or a
        reviewer asks for it
        """
        with repo:
            pr = _simple_init(repo)
            repo.post_status(pr.head, 'success')
            pr.post_comment(f'hansen r+ delegate={users["other"]} rebase-merge',
                            config["role_reviewer"]['token'])
        env.run_crons()
        pr_id = to_pr(env, pr)
        assert pr_id.staging_id

        staging_head = repo.commit('staging.master')
        with repo:
            repo.post_status('staging.master', 'failure')
        env.run_crons()
        assert pr_id.state == 'error'

        with repo:
            pr.post_comment('hansen r+ rebase-ff', config["role_reviewer"]['token'])
        env.run_crons()
        assert pr_id.state == 'error'
        assert pr.comments == [
            (users['reviewer'], f'hansen r+ delegate={users["other"]} rebase-merge'),
            seen(env, pr, users),
            (users['user'], 'Merge method set to rebase and merge, using the PR as merge commit message.'),
            (users['user'], '@{user} @{reviewer} staging failed: default'.format_map(users)),
            (users['reviewer'], 'hansen r+ rebase-ff'),
            (users['user'], "This PR is already reviewed, it's in error, you might want to `retry` it instead "
                            "(if you have already confirmed the error is not legitimate)."),
            (users['user'], 'Merge method set to rebase and fast-forward.'),
        ]
        assert pr_id.merge_method == 'rebase-ff'

        with repo:
            pr.post_comment('hansen retry', config['role_' + retrier]['token'])
        assert pr_id.state == 'ready'
        env.run_crons(None)

        staging_head2 = repo.commit('staging.master')
        assert staging_head2 != staging_head
        with repo:
            repo.post_status('staging.master', 'success')
        env.run_crons()
        assert pr_id.state == 'merged'

    def test_retry_again_message(self, env, repo, users, config, page):
        """ For a retried PR, the error message on the PR's page should be the
        later staging
        """
        with repo:
            pr = _simple_init(repo)
            repo.post_status(pr.head, 'success')
            pr.post_comment('hansen r+ delegate=%s rebase-merge' % users['other'],
                             config["role_reviewer"]['token'])
        env.run_crons()
        pr_id = to_pr(env, pr)
        assert pr_id.staging_id

        with repo:
            repo.post_status('staging.master', 'failure',
                             target_url='https://example.com/whocares')
        env.run_crons()
        assert pr_id.state == 'error'

        with repo:
            pr.post_comment('hansen retry', config['role_reviewer']['token'])
        env.run_crons(None)

        with repo:
            repo.post_status('staging.master', 'failure',
                             target_url='https://example.com/ohno')
        env.run_crons()
        assert pr_id.state == 'error'

        dangerbox = pr_page(page, pr).cssselect('.alert-danger span')
        assert dangerbox
        assert dangerbox[0].text == 'default (view more at https://example.com/ohno)'

    def test_retry_ignored(self, env, repo, users, config):
        """ Check feedback in case of ignored retry command on a non-error PR.
        """
        with repo:
            prx = _simple_init(repo)
            prx.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])
            prx.post_comment('hansen retry', config['role_reviewer']['token'])
        env.run_crons()

        assert prx.comments == [
            (users['reviewer'], 'hansen r+ rebase-ff'),
            (users['reviewer'], 'hansen retry'),
            seen(env, prx, users),
            (users['user'], "Merge method set to rebase and fast-forward."),
            (users['user'], "@{reviewer} retry makes no sense when the PR is not in error.".format_map(users)),
        ]

    @pytest.mark.parametrize('disabler', ['user', 'other', 'reviewer'])
    def test_retry_disable(self, env, repo, disabler, users, config):
        with repo:
            prx = _simple_init(repo)
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+ delegate=%s rebase-merge' % users['other'],
                             config["role_reviewer"]['token'])
        env.run_crons()
        pr_id = to_pr(env, prx)
        staging_id = pr_id.staging_id
        assert staging_id

        with repo:
            repo.post_status('staging.master', 'failure')
        env.run_crons()
        assert staging_id.state == 'failure'
        assert not staging_id.active
        assert pr_id.state == 'error'

        with repo:
            prx.post_comment('hansen r-', config['role_' + disabler]['token'])
        assert pr_id.state == 'validated'
        with repo:
            repo.make_commits(prx.ref, Commit('third', tree={'m': 'c3'}))
            # just in case, apparently in some case the first post_status uses the old head...
        with repo:
            repo.post_status(prx.head, 'success')
        env.run_crons()
        assert pr_id.state == 'validated'

class TestMergeMethod:
    """
    if event['pull_request']['commits'] == 1, "squash" (/rebase); otherwise
    regular merge
    """
    def test_pr_single_commit(self, repo, env, config):
        """ If single commit, default to rebase & FF
        """
        with repo:
            m, m2 = repo.make_commits(
                None,
                Commit('initial', tree={'m': 'm'}),
                Commit('second', tree={'m2': 'm2'}),
                ref='heads/master',
            )

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        assert to_pr(env, prx).squash

        env.run_crons()
        assert to_pr(env, prx).staging_id

        staging = repo.commit('staging.master')
        assert not repo.is_ancestor(prx.head, of=staging.id),\
            "the pr head should not be an ancestor of the staging branch in a squash merge"
        assert repo.read_tree(staging) == {
            'm': 'c1', 'm2': 'm2',
        }, "the tree should still be correctly merged"
        assert staging.parents == [m2],\
            "dummy commit aside, the previous master's tip should be the sole parent of the staging commit"

        with repo:
            repo.post_status('staging.master', 'success')
        env.run_crons()
        pr = to_pr(env, prx)
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
            repo.post_status(pr.head, 'success')
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
            m, _ = repo.make_commits(
                None,
                Commit('initial', tree={'m': 'm'}),
                Commit('second', tree={'m2': 'm2'}),
                ref='heads/master',
            )

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
        pr = to_pr(env, prx)
        assert pr.squash, "a PR with a single commit should be squashed"

        with repo:
            repo.make_commits(prx.ref, Commit('second2', tree={'m': 'c2'}), ref=prx.ref, make=False)
        assert not pr.squash, "a PR with a single commit should not be squashed"

    def test_pr_reset_to_single_commit(self, repo, env):
        """
        If a PR starts at >1 commits and is reset back to 1, the PR should be
        re-flagged as squash
        """
        with repo:
            [m, _] = repo.make_commits(
                None,
                Commit('initial', tree={'m': 'm'}),
                Commit('second', tree={'m2': 'm2'}),
                ref='heads/master',
            )

            [_, c2] = repo.make_commits(
                m,
                Commit('first', tree={'m': 'c1'}),
                Commit('second2', tree={'m': 'c2'}),
            )
            prx = repo.make_pr(title='title', body='body', target='master', head=c2)
        pr = to_pr(env, prx)
        pr.merge_method = 'rebase-merge'
        assert not pr.squash, "a PR with a single commit should not be squashed"

        with repo:
            repo.make_commits(m, Commit('fixup', tree={'m': 'c2'}), ref=prx.ref, make=False)
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
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        # wait hook and run crons before status to works around a race in
        # runbot mode (with high parallelism)
        env.run_crons()
        with repo:
            repo.post_status(prx.head, 'success')
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
            _, m1, _ = repo.make_commits(
                None,
                Commit('M0', tree={'m': '0'}),
                Commit('M1', tree={'m': '1'}),
                Commit('M2', tree={'m': '2'}),
                ref='heads/master',
            )

            _, b1 = repo.make_commits(
                m1,
                Commit('B0', tree={'m': '1', 'b': '0'}),
                Commit('B1', tree={'m': '1', 'b': '1'}),
            )
            prx = repo.make_pr(title='title', body='body', target='master', head=b1)
        pr = to_pr(env, prx)
        with repo:
            repo.post_status(prx.head, 'success')

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
            _, m1, m2 = repo.make_commits(
                None,
                Commit('M0', tree={'m': '0'}),
                Commit('M1', tree={'m': '1'}),
                Commit('M2', tree={'m': '2'}),
                ref='heads/master',
            )

            # test commit ordering issue while at it: github sorts commits on
            # author.date instead of doing so topologically which is absolutely
            # not what we want
            committer = {'name': 'a', 'email': 'a', 'date': '2018-10-08T11:48:43Z'}
            author0 = {'name': 'a', 'email': 'a', 'date': '2018-10-01T14:58:38Z'}
            author1 = {'name': 'a', 'email': 'a', 'date': '2015-10-01T14:58:38Z'}
            b0, b1 = repo.make_commits(
                m1,
                Commit('B0', author=author0, committer=committer, tree={'m': '1', 'b': '0'}),
                Commit('B1', author=author1, committer=committer, tree={'m': '1', 'b': '1'}),
            )
            prx = repo.make_pr(title='title', body='body', target='master', head=b1)
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
        env.run_crons()

        pr_id = to_pr(env, prx)
        # create a dag (msg:str, parents:set) from the log
        staging = log_to_node(repo.log('staging.master'))
        # then compare to the dag version of the right graph
        nm2 = node('M2', node('M1', node('M0')))
        nb1 = node(part_of('B1', pr_id), node(part_of('B0', pr_id), nm2))
        reviewer = get_partner(env, users["reviewer"]).formatted_email
        merge_head = (
            f'title\n\nbody\n\ncloses {pr_id.display_name}\n\nSigned-off-by: {reviewer}',
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
            repo.post_status('staging.master', 'success')
        env.run_crons()

        pr = to_pr(env, prx)
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
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])
        env.run_crons()

        pr_id = to_pr(env, prx)
        # create a dag (msg:str, parents:set) from the log
        staging = log_to_node(repo.log('staging.master'))
        # then compare to the dag version of the right graph
        nm2 = node('M2', node('M1', node('M0')))
        reviewer = get_partner(env, users["reviewer"]).formatted_email
        nb1 = node(f'B1\n\ncloses {pr_id.display_name}\n\nSigned-off-by: {reviewer}',
                   node(part_of('B0', pr_id), nm2))
        assert staging == nb1

        with repo:
            repo.post_status('staging.master', 'success')
        env.run_crons()

        pr = to_pr(env, prx)
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
            [m] = repo.make_commits(None, Commit("M", tree={'a': 'a'}), ref='heads/master')

            [c0] = repo.make_commits(m, Commit('C0', tree={'a': 'b'}))
            prx = repo.make_pr(title="gibberish", body="blahblah", target='master', head=c0)
        env.run_crons(None)

        with repo:
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+ merge', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('staging.master', 'success')
        env.run_crons()

        master = repo.commit('heads/master')
        assert master.parents == [m, prx.head], \
            "master's parents should be the old master & the PR head"

        m = node('M')
        c0 = node('C0', m)
        reviewer = get_partner(env, users["reviewer"]).formatted_email
        expected = node('gibberish\n\nblahblah\n\ncloses {}#{}'
                        '\n\nSigned-off-by: {}'.format(repo.name, prx.number, reviewer), m, c0)
        assert log_to_node(repo.log('heads/master')), expected
        pr = to_pr(env, prx)
        assert json.loads(pr.commits_map) == {
            prx.head: prx.head,
            '': master.id
        }

    def test_unrebase_emptymessage(self, repo, env, users, config):
        """ When merging between master branches (e.g. forward port), the PR
        may have only a title
        """
        with repo:
            [m] = repo.make_commits(None, Commit("M", tree={'a': 'a'}), ref='heads/master')

            [c0] = repo.make_commits(m, Commit('C0', tree={'a': 'b'}))
            prx = repo.make_pr(title="gibberish", body=None, target='master', head=c0)
        env.run_crons(None)

        with repo:
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+ merge', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('staging.master', 'success')
        env.run_crons()

        master = repo.commit('heads/master')
        assert master.parents == [m, prx.head], \
            "master's parents should be the old master & the PR head"

        m = node('M')
        c0 = node('C0', m)
        reviewer = get_partner(env, users["reviewer"]).formatted_email
        expected = node('gibberish\n\ncloses {}#{}'
                        '\n\nSigned-off-by: {}'.format(repo.name, prx.number, reviewer), m, c0)
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
            repo.post_status(pr.head, 'success')
            pr.post_comment('hansen r+ merge', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('staging.master', 'success')
        env.run_crons()

        head = repo.commit('heads/master')
        assert head.message == textwrap.dedent(f"""\
        title

        first

        closes {repo.name}#{pr.number}

        Signed-off-by: {reviewer}
        """).strip(), "should not contain the content which follows the thematic break"

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
            repo.post_status(pr.head, 'success')
            pr.post_comment('hansen r+ merge', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('staging.master', 'success')
        env.run_crons()

        head = repo.commit('heads/master')
        assert head.message == textwrap.dedent(f"""\
        title

        Title
        ---
        This is some text

        Title 2
        -------
        This is more text

        closes {repo.name}#{pr.number}

        Signed-off-by: {reviewer}
        """).strip(), "should not break the SETEX titles"

    def test_rebase_no_edit(self, repo, env, users, config):
        """ Only the merge messages should be de-breaked
        """
        reviewer = get_partner(env, users["reviewer"]).formatted_email
        with repo:
            root = repo.make_commits(None, Commit("root", tree={'a': 'a'}), ref='heads/master')

            repo.make_commits(root, Commit('Commit\n\nfirst\n***\nsecond', tree={'a': 'b'}), ref='heads/change')
            pr = repo.make_pr(title="PR", body='first\n***\nsecond',
                              target='master', head='change')
            repo.post_status(pr.head, 'success')
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('staging.master', 'success')
        env.run_crons()

        head = repo.commit('heads/master')
        assert head.message == textwrap.dedent(f"""\
        Commit

        first
        ***
        second

        closes {repo.name}#{pr.number}

        Signed-off-by: {reviewer}
        """).strip(), "squashed / rebased messages should not be stripped"

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
            repo.post_status(pr.head, 'success')
            pr.post_comment('hansen rebase-ff r+', config['role_reviewer']['token'])
        env.run_crons()

        pr_id = to_pr(env, pr)
        assert pr_id.staging_id # check PR is staged


        reviewer = get_partner(env, users["reviewer"]).formatted_email
        staging_head = repo.commit('staging.master')
        assert staging_head.message == f"""\
thing: thong

closes {pr_id.display_name}

Signed-off-by: {reviewer}"""
        assert repo.commit(staging_head.parents[0]).message == f"""\
Some: thing

is odd

Part-of: {pr_id.display_name}
Signed-off-by: {reviewer}"""

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
            [m1, m2] = repo.make_commits(
                None,
                Commit("M1", tree={'a': '0'}),
                Commit("M2", tree={'a': '1'}),
                ref='heads/master',
            )

            [c0] = repo.make_commits(m1, Commit('C0', tree={'b': '2'}))
            [c1] = repo.make_commits([c0, m2], Commit('C1', tree={'a': '1', 'b': '2'}))
            prx = repo.make_pr(title="T", body="TT", target='master', head=c1)
        env.run_crons()

        with repo:
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+ merge', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('staging.master', 'success')
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
            m1, m2 = repo.make_commits(
                None,
                Commit("M1", tree={'a': '0'}),
                Commit("M2", tree={'a': '1'}),
                ref='heads/master',
            )

            [b0] = repo.make_commits(m1, Commit('B0', tree={'bb': 'bb'}))

            [c0] = repo.make_commits(m1, Commit('C0', tree={'b': '2'}))
            [c1] = repo.make_commits([c0, b0], Commit('C1', tree={'a': '0', 'b': '2', 'bb': 'bb'}))
            prx = repo.make_pr(title="T", body="TT", target='master', head=c1)
        env.run_crons()

        with repo:
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+ merge', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('staging.master', 'success')
        env.run_crons()

        master = repo.commit('heads/master')
        assert master.parents == [m2, c1]
        assert repo.read_tree(master) == {'a': '1', 'b': '2', 'bb': 'bb'}

        m1 = node('M1')
        reviewer = get_partner(env, users["reviewer"]).formatted_email
        expected = node(
            'T\n\nTT\n\ncloses {}#{}\n\nSigned-off-by: {}'.format(repo.name, prx.number, reviewer),
            node('M2', m1),
            node('C1', node('C0', m1), node('B0', m1))
        )
        assert log_to_node(repo.log('heads/master')), expected

    def test_squash_merge(self, project, repo, env, config, users):
        other_user = {
            'name': config['role_other']['name'],
            'email': config['role_other']['email'],
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
            repo.post_status('other', 'success')

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
            repo.post_status('other2', 'success')
        env.run_crons()

        with repo: # comments sequencing
            pr1.post_comment('hansen r+ squash', config['role_reviewer']['token'])
            pr2.post_comment('hansen r+ squash', config['role_reviewer']['token'])
        env.run_crons()

        with repo:
            repo.post_status('staging.master', 'success')
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

Signed-off-by: {get_partner(env, users["reviewer"]).formatted_email}\
"""
        assert one['commit']['committer']['name'] == a_user['name']
        assert one['commit']['committer']['email'] == a_user['email']
        commit_date = datetime.datetime.strptime(one['commit']['committer']['date'], '%Y-%m-%dT%H:%M:%SZ')
        # using timestamp (and working in seconds) because `pytest.approx`
        # silently fails on datetimes (#8395)
        assert commit_date.timestamp() == pytest.approx(time.time(), abs=5*60), \
            "the commit date of the merged commit should be about now, despite" \
            " the source commit being >20 years old"

        # since there are two authors & two committers on pr2, the auhor and
        # committer of a squash commit should be reset to the bot's identity
        bot_id = project.github_name, project.github_email
        getid = itemgetter('name', 'email')
        assert getid(two['commit']['committer']) == bot_id
        assert getid(two['commit']['author']) == bot_id
        assert two['commit']['message'] == f"""second pr

closes {pr2_id.display_name}

Signed-off-by: {get_partner(env, users["reviewer"]).formatted_email}
Co-authored-by: {a_user['name']} <{a_user['email']}>
Co-authored-by: {other_user['name']} <{other_user['email']}>\
"""
        assert repo.read_tree(repo.commit(two['sha'])) == {
            'a': '0',
            'b': '0',
            'x': '1',
        }


class TestPRUpdate:
    """ Pushing on a PR should update the HEAD except for merged PRs, it
    can have additional effect (see individual tests)
    """
    @pytest.fixture(autouse=True)
    def master(self, repo):
        with repo:
            [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref="heads/master")
        return m

    def test_update_opened(self, env, repo):
        with repo:
            [c] = repo.make_commits("master", Commit('first', tree={'m': 'c1'}))
            prx = repo.make_pr(target='master', head=c)

        pr = to_pr(env, prx)
        assert pr.head == c
        # alter & push force PR entirely
        with repo:
            [c2] = repo.make_commits("master", Commit('first', tree={'m': 'cc'}))
            repo.update_ref(prx.ref, c2, force=True)
        assert pr.head == c2

    def test_update_validated(self, env, repo):
        """ Should reset to opened
        """
        with repo:
            [c] = repo.make_commits("master", Commit('first', tree={'m': 'c1'}))
            pr = repo.make_pr(target='master', head=c)
            repo.post_status(c, 'success')
        env.run_crons()

        pr_id = to_pr(env, pr)
        assert pr_id.head == c
        assert pr_id.state == 'validated'

        with repo:
            [c2] = repo.make_commits("master", Commit('first', tree={'m': 'cc'}))
            repo.update_ref(pr.ref, c2, force=True)
        assert pr_id.head == c2
        assert pr_id.state == 'opened'

    def test_update_approved(self, env, repo, config):
        with repo:
            [c] = repo.make_commits("master", Commit('fist', tree={'m': 'c1'}))
            prx = repo.make_pr(target='master', head=c)
            prx.post_comment('hansen r+', config['role_reviewer']['token'])

        pr = to_pr(env, prx)
        assert pr.head == c
        assert pr.state == 'approved'

        with repo:
            [c2] = repo.make_commits("master", Commit('first', tree={'m': 'cc'}))
            repo.update_ref(prx.ref, c2, force=True)
        assert pr.head == c2
        assert pr.state == 'opened'

    def test_update_ready(self, env, repo, config):
        """ Should reset to opened
        """
        with repo:
            [c] = repo.make_commits("master", Commit('fist', tree={'m': 'c1'}))
            prx = repo.make_pr(target='master', head=c)
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()
        pr = to_pr(env, prx)
        assert pr.head == c
        assert pr.state == 'ready'

        with repo:
            [c2] = repo.make_commits(c, Commit('first', tree={'m': 'cc'}))
            repo.update_ref(prx.ref, c2, force=True)
        assert pr.head == c2
        assert pr.state == 'opened'

    def test_update_staged(self, env, repo, config):
        """ Should cancel the staging & reset PR to opened
        """
        with repo:
            [c] = repo.make_commits("master", Commit('fist', tree={'m': 'c1'}))
            prx = repo.make_pr(target='master', head=c)
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])

        env.run_crons()
        pr = to_pr(env, prx)
        assert pr.state == 'ready'
        assert pr.staging_id

        with repo:
            [c2] = repo.make_commits(c, Commit('first', tree={'m': 'cc'}))
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
            repo.make_commits("master", Commit('first', tree={'1': '1'}), ref="heads/p1")
            prx1 = repo.make_pr(target='master', head='p1')
            repo.post_status(prx1.head, 'success')
            prx1.post_comment('hansen r+', config['role_reviewer']['token'])

            [c] = repo.make_commits("master", Commit('first', tree={'2': '2'}), ref="heads/p2")
            prx2 = repo.make_pr(target='master', head='p2')
            repo.post_status(prx2.head, 'success')
            prx2.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        pr1, pr2 = env['runbot_merge.pull_requests'].search([], order='number')
        assert pr1.number == prx1.number
        assert pr2.number == prx2.number
        assert pr1.staging_id == pr2.staging_id
        s0 = pr1.staging_id

        with repo:
            repo.post_status('staging.master', 'failure')
        env.run_crons()

        assert pr1.staging_id and pr1.staging_id != s0, "pr1 should have been re-staged"
        assert not pr2.staging_id, "pr2 should not"
        # TODO: remote doesn't currently handle env context so can't mess
        #       around using active_test=False
        assert env['runbot_merge.split'].search([])

        with repo:
            [c2] = repo.make_commits(c, Commit('second', tree={'2': '22'}))
            repo.update_ref(prx2.ref, c2, force=True)
        # probably not necessary ATM but...
        env.run_crons()

        assert pr2.state == 'opened', "state should have been reset"
        assert not env['runbot_merge.split'].search([]), "there should be no split left"

    def test_update_error(self, env, repo, config):
        with repo:
            [c] = repo.make_commits("master", Commit('fist', tree={'m': 'c1'}))
            prx = repo.make_pr(target='master', head=c)
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()
        pr = to_pr(env, prx)
        assert pr.state == 'ready'
        assert pr.staging_id

        with repo:
            repo.post_status('staging.master', 'failure')
        env.run_crons()
        assert not pr.staging_id
        assert pr.state == 'error'

        with repo:
            [c2] = repo.make_commits(c, Commit('first', tree={'m': 'cc'}))
            repo.update_ref(prx.ref, c2, force=True)
        assert pr.head == c2
        assert pr.state == 'opened'

    def test_unknown_pr(self, env, repo):
        with repo:
            [m, c] = repo.make_commits(
                None,
                Commit('initial', tree={'m': 'm'}),
                Commit('first', tree={'m': 'c1'}),
            )
            repo.make_ref('heads/1.0', m)
            prx = repo.make_pr(title='title', body='body', target='1.0', head=c)
        with pytest.raises(TimeoutError):
            to_pr(env, prx)

        env['runbot_merge.project'].search([]).write({
            'branch_ids': [(0, 0, {'name': '1.0'})]
        })

        with repo:
            [c2] = repo.make_commits(c, Commit('second', tree={'m': 'c2'}))
            repo.update_ref(prx.ref, c2, force=True)

        with pytest.raises(TimeoutError):
            to_pr(env, prx)

    def test_update_to_ci(self, env, repo, status_mode):
        """ If a PR is updated to a known-valid commit, it should be
        validated
        """
        if status_mode == 'runbot':
            pytest.xfail("No such thing as a valid commit in runbot status mode")

        with repo:
            [c] = repo.make_commits("master", Commit('fist', tree={'m': 'c1'}))
            [c2] = repo.make_commits("master", Commit('first', tree={'m': 'cc'}))
            repo.post_status(c2, 'success')
        env.run_crons()

        with repo:
            prx = repo.make_pr(target='master', head=c)
        pr = to_pr(env, prx)
        assert pr.head == c
        assert pr.state == 'opened'

        with repo:
            repo.update_ref(prx.ref, c2, force=True)
        assert pr.head == c2
        assert pr.state == 'validated'

    def test_update_missed(self, env, repo, config, users, status_mode):
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
        env.ref('runbot_merge.cron_validate').active = False
        with repo:
            [c] = repo.make_commits(None, repo.Commit('m', tree={'a': '0'}), ref='heads/master')
            repo.make_ref('heads/somethingelse', c)

            [c] = repo.make_commits(
                'master', repo.Commit('title \n\nbody', tree={'a': '1'}), ref='heads/abranch')
            pr = repo.make_pr(target='master', head='abranch')
            repo.post_status(pr.head, 'success')
            pr.post_comment('hansen r+', config['role_reviewer']['token'])

        env.run_crons()
        pr_id = to_pr(env, pr)
        env.run_crons(None)
        assert pr_id.message == 'title\n\nbody'
        assert pr_id.state == 'ready'
        old_reviewer = pr_id.reviewed_by

        with repo:
            # can't push a second commit because then the staging fails due
            # to the PR *actually* having more than 1 commit and thus needing
            # a configuration
            [c2] = repo.make_commits('heads/master', repo.Commit('c2', tree={'a': '2'}))
            if status_mode != 'runbot':
                repo.post_status(c2, 'success')
            # simulate missing a notification
            with repo.disable_hooks():
                repo.update_ref(pr.ref, c2, force=True)

        # we missed the update notification so the db should still be at c and
        # in a "ready" state
        assert pr_id.head == c
        assert pr_id.state == "ready"
        assert pr_id.reviewed_by == old_reviewer

        # do some more fucking up of the PR
        other = env['runbot_merge.branch'].create({
            'name': 'somethingelse',
            'project_id': env['runbot_merge.project'].search([]).id,
        })
        pr_id.write({'message': "Something else", 'target': other.id})

        env.run_crons()

        # the PR should not get merged, and should be updated
        assert pr_id.head == c2
        if status_mode == 'runbot':
            assert pr_id.state == 'opened'
        else:
            assert pr_id.state == 'validated'
        assert pr_id.message == 'title\n\nbody'
        assert pr_id.target.name == 'master'
        assert pr.comments[-1]['body'] == f"""\
@{users['user']} we apparently missed updates to this PR and tried to stage it in a state \
which might not have been approved.

The properties Head, Target, Message were not correctly synchronized and have been updated.

<details><summary>differences</summary>

```diff
  Head:
- {c}
+ {c2}
  
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
"""

        # if the head commit doesn't change, that part should still be valid
        with repo:
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
            repo.post_status(c2, 'success')  # redundant in !runbot case but...
        assert pr_id.state == 'ready'
        pr_id.write({'message': 'wrong'})
        env.run_crons()

        assert pr_id.message == 'title\n\nbody'
        assert pr_id.state == 'validated'
        assert pr.comments[-1]['body'] == f"""\
@{users['user']} we apparently missed updates to this PR and tried to stage it in a state \
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
"""

        pr_id.write({
            'head': c,
            'reviewed_by': old_reviewer.id,
            'message': "Something else",
            'target': other.id,
            'draft': True,
        })
        with repo:
            pr.post_comment('hansen check')
        env.run_crons()
        assert pr_id.head == c2
        if status_mode == 'runbot':
            assert pr_id.state == 'opened'
        else:
            assert pr_id.state == 'validated'
        assert pr_id.message == 'title\n\nbody' # the commit's message was used for the PR
        assert pr_id.target.name == 'master'
        assert not pr_id.draft
        assert pr.comments[-1] == (
            users['user'],
            f"Updated target, squash, message. Updated {pr_id.display_name} to ready. Updated to {c2}."
        )

        pr_id.write({'squash': False})
        with repo:
            pr.post_comment('hansen check')
        env.run_crons()
        assert pr_id.squash

    def test_update_closed(self, env, repo, config, status_mode):
        with repo:
            [c] = repo.make_commits("master", repo.Commit('first', tree={'m': 'm3'}), ref='heads/abranch')
            pr = repo.make_pr(target='master', head=c)
            pr.post_comment("hansen r+", config['role_reviewer']['token'])
        env.run_crons()

        pr_id = to_pr(env, pr)
        assert pr_id.state == 'approved'
        assert pr_id.head == c
        assert pr_id.squash
        assert pr_id.reviewed_by

        with repo:
            pr.close()
        assert pr.state == 'closed'
        assert pr.head == c
        assert not pr_id.reviewed_by
        assert pr_id.squash

        with repo:
            [c2] = repo.make_commits(c, Commit('xxx', tree={'m': 'm4'}))
            repo.update_ref(pr.ref, c2)
            if status_mode != 'runbot':
                repo.post_status(c2, "success")

        assert pr_id.state == 'closed'
        assert pr_id.head == c
        assert not pr_id.reviewed_by
        assert pr_id.squash

        with repo:
            pr.open()
        if status_mode == 'runbot':
            assert pr_id.state == 'opened'
        else:
            assert pr_id.state == 'validated'
        assert pr_id.head == c2
        assert not pr_id.reviewed_by
        assert not pr_id.squash

    def test_update_incorrect_commits_count(self, port, env, project, repo, config, users, partners, status_mode):
        """This is not a great test but it aims to kinda sorta simulate the
        behaviour when a user retargets and updates a PR at about the same time:
        github can send the hooks in the wrong order, which leads to the correct
        base and head but can lead to the wrong squash status.
        """
        project.write({
            'branch_ids': [(0, 0, {
                'name': 'xxx',
            })]
        })
        with repo:
            [c] = repo.make_commits("master", Commit("c", tree={"m": "n"}), ref="heads/thing")
            pr = repo.make_pr(target='master', head='thing')

        pr_id = to_pr(env, pr)
        pr_id.head = '0'*40
        with requests.Session() as s:
            r = s.post(
                f"http://localhost:{port}/runbot_merge/hooks",
                headers={
                    "X-Github-Event": "pull_request",
                },
                json={
                    'action': 'synchronize',
                    'sender': {
                        'login': users['user'],
                    },
                    'repository': {
                        'full_name': repo.name,
                    },
                    'pull_request': {
                        'number': pr.number,
                        'head': {'sha': c},
                        'title': "c",
                        'commits': 40123,
                        'base': {
                            'ref': 'xxx',
                            'repo': {
                                'full_name': repo.name,
                            },
                        }
                    }
                }
            )
            r.raise_for_status()
        assert pr_id.head == c, "the head should have been updated"
        assert not pr_id.squash, "the wrong count should be used"

        with repo:
            pr.post_comment("hansen r+", config['role_reviewer']['token'])
        with repo:
            repo.post_status(c, 'success')
        env.run_crons()
        assert not pr_id.blocked
        if status_mode == 'runbot':
            statuses_msg = '<p>statuses updated by runbot</p>'
        else:
            statuses_msg = f'<p>statuses changed on {c}</p>'
        assert pr_id.message_ids[::-1].mapped(lambda m: (
            ((m.subject or '') + '\n\n' + m.body).strip(),
            list(map(read_tracking_value, m.tracking_value_ids)),
        )) == [
            ('<p>Pull Request created</p>', []),
            ('', [('head', c, '0'*40)]),
            ('', [('head', '0'*40, c), ('squash', 1, 0)]),
            ('', [('reviewed_by', '', partners['reviewer'].name), ('state', 'Opened', 'Approved')]),
            (statuses_msg, [('state', 'Approved', 'Ready')]),
        ]
        assert pr_id.staging_id
        with repo:
            repo.post_status('staging.master', 'success')
        env.run_crons()
        assert pr_id.merge_date

class TestReviewing:
    def test_reviewer_rights(self, env, repo, users, config):
        """Only users with review rights will have their r+ (and other
        attributes) taken in account
        """
        with repo:
            m, _ = repo.make_commits(
                None,
                Commit('initial', tree={'m': 'm'}),
                Commit('second', tree={'m2': 'm2'}),
                ref='heads/master',
            )

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+', config['role_other']['token'])
        env.run_crons()

        assert to_pr(env, prx).state == 'validated'
        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        assert to_pr(env, prx).state == 'ready'
        # second r+ to check warning
        with repo:
            prx.post_comment('hansen r+', config['role_reviewer']['token'])

        env.run_crons()
        assert prx.comments == [
            (users['other'], 'hansen r+'),
            seen(env, prx, users),
            (users['user'], f"@{users['other']} you can't review+."),
            (users['reviewer'], 'hansen r+'),
            (users['reviewer'], 'hansen r+'),
            (users['user'], "This PR is already reviewed, reviewing it again is useless."),
        ]

    def test_self_review_fail(self, env, repo, users, config):
        """ Normal reviewers can't self-review
        """
        reviewer = config['role_reviewer']['token']
        with repo:
            [m, _] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), Commit('second', tree={'m2': 'm2'}), ref='heads/master')
            with repo.fork(token=reviewer) as f:
                f.make_commits(m, Commit('first', tree={'m': 'c1'}), ref='heads/change')
            prx = repo.make_pr(title='title', body='body', target='master', head=f'{f.owner}:change', token=reviewer)
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+', reviewer)
        env.run_crons()

        assert prx.user == users['reviewer']
        assert to_pr(env, prx).state == 'validated'

        env.run_crons()
        assert prx.comments == [
            (users['reviewer'], 'hansen r+'),
            seen(env, prx, users),
            (users['user'], "@{} you can't review+.".format(users['reviewer'])),
        ]

    def test_self_review_success(self, env, repo, users, config):
        """ Some users are allowed to self-review
        """
        self_reviewer = config['role_self_reviewer']['token']
        with repo:
            [m, _] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), Commit('second', tree={'m2': 'm2'}), ref='heads/master')
            with repo.fork(token=self_reviewer) as f:
                f.make_commits(m, Commit('first', tree={'m': 'c1'}), ref='heads/change')
            prx = repo.make_pr(title='title', body='body', target='master', head=f'{f.owner}:change', token=self_reviewer)
            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen r+', self_reviewer)
        env.run_crons()

        assert prx.user == users['self_reviewer']
        assert to_pr(env, prx).state == 'ready'

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
            m, _ = repo.make_commits(
                None,
                Commit('initial', tree={'m': 'm'}),
                Commit('second', tree={'m2': 'm2'}),
                ref='heads/master',
            )

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
            repo.post_status(prx.head, 'success')
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
            m, _ = repo.make_commits(
                None,
                Commit('initial', tree={'m': 'm'}),
                Commit('second', tree={'m2': 'm2'}),
                ref='heads/master',
            )

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
            repo.post_status(prx.head, 'success')
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
            [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

            [c] = repo.make_commits(m, Commit('first', tree={'m': 'c'}))
            prx = repo.make_pr(title='title', body=None, target='master', head=c)
            prx.post_comment('hansen delegate=foo,@bar,#baz', config['role_reviewer']['token'])

        pr = to_pr(env, prx)

        assert {d.github_login for d in pr.delegates} == {'foo', 'bar', 'baz'}

    def test_actual_review(self, env, repo, config):
        """ treat github reviews as regular comments
        """
        with repo:
            m, _ = repo.make_commits(
                None,
                Commit('initial', tree={'m': 'm'}),
                Commit('second', tree={'m2': 'm2'}),
                ref='heads/master',
            )

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
        pr = to_pr(env, prx)

        with repo:
            prx.post_review('COMMENT', "hansen priority", config['role_reviewer']['token'])
        assert pr.priority == 'priority'
        assert pr.state == 'opened'

        with repo:
            prx.post_review('APPROVE', "hansen default", config['role_reviewer']['token'])
        assert pr.priority == 'default'
        assert pr.state == 'opened'

        with repo:
            prx.post_review('REQUEST_CHANGES', 'hansen priority', config['role_reviewer']['token'])
        assert pr.priority == 'priority'
        assert pr.state == 'opened'

        with repo:
            prx.post_review('COMMENT', 'hansen r+', config['role_reviewer']['token'])
        assert pr.priority == 'priority'
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
            (users['user'], f"@{users['user']} I must know your email before you can review PRs. Please contact an administrator."),
        ]
        user_partner.fetch_github_email()
        assert user_partner.email
        with repo:
            pr.post_comment('hansen r+', config['role_user']['token'])
        env.run_crons()
        assert to_pr(env, pr).state == 'approved'

    @pytest.mark.usefixtures("reviewer_admin")
    def test_skipchecks(self, env, repo, users, config):
        """Skipcheck makes the PR immediately ready (if it's not in error or
        something)
        """
        with repo:
            [m, _] = repo.make_commits(
                None,
                Commit("initial", tree={'m': 'm'}),
                Commit("second", tree={"m2": "m2"}),
                ref="heads/master"
            )

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            pr = repo.make_pr(title='title', target='master', head=c1)
            pr.post_comment('hansen skipchecks', config['role_reviewer']['token'])
        env.run_crons()

        pr_id = to_pr(env, pr)
        # assert pr_id.state == 'ready'
        assert not pr_id.blocked
        # since the pr is not blocked it should have been staged by the relevant cron
        assert pr_id.staging_id

class TestUnknownPR:
    """ Sync PRs initially looked excellent but aside from the v4 API not
    being stable yet, it seems to have greatly regressed in performances to
    the extent that it's almost impossible to sync odoo/odoo today: trying to
    fetch more than 2 PRs per query will fail semi-randomly at one point, so
    fetching all 15000 PRs takes hours

    => instead, create PRs on the fly when getting notifications related to
       valid but unknown PRs
    """
    def test_rplus_unknown(self, repo, env, config, users, status_mode):
        with repo:
            m, _ = repo.make_commits(
                None,
                Commit('initial', tree={'m': 'm'}),
                Commit('second', tree={'m2': 'm2'}),
                ref='heads/master',
            )

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
            repo.post_status(prx.head, 'success', target_url="http://example.org/wheee")
        env.run_crons()

        # assume an unknown but ready PR: we don't know the PR or its head commit
        to_pr(env, prx).unlink()
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

        if status_mode != 'runbot':
            c = env['runbot_merge.commit'].search([('sha', '=', prx.head)])
            assert json.loads(c.statuses) == {
                'default': {
                    'state': 'success',
                    'target_url': 'http://example.org/wheee',
                    'description': None,
                    'updated_at': matches("$$"),
                }
            }
        assert prx.comments == [
            seen(env, prx, users),
            (users['reviewer'], 'hansen r+'),
            (users['reviewer'], 'hansen r+'),
            seen(env, prx, users),
            (users['user'], f"@{users['reviewer']} I didn't know about this PR and had to "
                            "retrieve its information, you may have to "
                            "re-approve it as I didn't see previous commands."),
        ]

        pr = to_pr(env, prx)
        if status_mode == 'runbot':
            assert pr.state == 'opened'
        else:
            assert pr.state == 'validated'

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
            (users['user'], f"@{users['reviewer']} I didn't know about this PR and had to retrieve "
                            "its information, you may have to re-approve it "
                            "as I didn't see previous commands."),
        ]

    def test_close_unknown_unmanaged(self, env, repo, users, config):
        """If an "unknown PR" is *closed*, it should be saved as closed but not
        commented on, because that's unnecessary spam.
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
        assert pr.comments == [seen(env, pr, users)]

        to_pr(env, pr).unlink()
        env['runbot_merge.commit'].search([('sha', '=', pr.head)]).unlink()

        with repo:
            pr.close()

        Fetch = env['runbot_merge.fetch_job']
        fetches = Fetch.search([('repository', '=', repo.name), ('number', '=', pr.number)])
        assert len(fetches) == 1, f"expected one fetch for {pr.number}, found {len(fetches)}"

        env.run_crons('runbot_merge.fetch_prs_cron', context={
            'current_date': datetime.datetime.now() + datetime.timedelta(minutes=10),
        })
        env.run_crons()
        assert not Fetch.search([('repository', '=', repo.name), ('number', '=', pr.number)])

        assert to_pr(env, pr).state == 'closed'
        assert pr.comments == [seen(env, pr, users)]


    def test_close_unknown_disabled(self, env, repo, users, config):
        """If an "unknown PR" on an disabled branch is *closed*, it should be
        saved as closed but not commented on, because that's unnecessary spam.
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
        assert pr.comments == [seen(env, pr, users)]

        to_pr(env, pr).unlink()
        env['runbot_merge.commit'].search([('sha', '=', pr.head)]).unlink()
        env['runbot_merge.branch'].search([('name', '=', 'master')]).active = False

        with repo:
            pr.close()

        Fetch = env['runbot_merge.fetch_job']
        fetches = Fetch.search([('repository', '=', repo.name), ('number', '=', pr.number)])
        assert len(fetches) == 1, f"expected one fetch for {pr.number}, found {len(fetches)}"

        env.run_crons('runbot_merge.fetch_prs_cron', context={
            'current_date': datetime.datetime.now() + datetime.timedelta(minutes=10),
        })
        env.run_crons()
        assert not Fetch.search([('repository', '=', repo.name), ('number', '=', pr.number)])

        assert to_pr(env, pr).state == 'closed'
        assert pr.comments == [seen(env, pr, users)]

    def test_rplus_unmanaged(self, env, repo, users, config):
        """ r+ on an unmanaged target should notify about
        """
        with repo:
            m, _ = repo.make_commits(
                None,
                Commit('initial', tree={'m': 'm'}),
                Commit('second', tree={'m2': 'm2'}),
                ref='heads/branch',
            )

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            prx = repo.make_pr(title='title', body='body', target='branch', head=c1)
            prx.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons('runbot_merge.fetch_prs_cron')

        assert prx.comments == [
            (users['reviewer'], 'hansen r+'),
            (users['user'], f"This PR targets the un-managed branch {repo.name}:branch, it needs to be retargeted before it can be merged."),
            (users['user'], "Branch `branch` is not within my remit, imma just ignore it."),
        ]

    def test_rplus_review_unmanaged(self, env, repo, users, config):
        """ r+ reviews can take a different path than comments
        """
        with repo:
            m, _ = repo.make_commits(
                None,
                Commit('initial', tree={'m': 'm'}),
                Commit('second', tree={'m2': 'm2'}),
                ref='heads/branch',
            )

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            prx = repo.make_pr(title='title', body='body', target='branch', head=c1)

            prx.post_review('APPROVE', 'hansen r+', config['role_reviewer']['token'])
        env.run_crons(
            'runbot_merge.fetch_prs_cron',
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
            [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

            [c] = repo.make_commits(m, Commit('first', tree={'m': 'c'}))
            prx = repo.make_pr(title='title', body=None, target='master', head=c)

        pr = to_pr(env, prx)
        assert pr.state == 'opened'

        with repo:
            prx.post_comment('%s r+' % botname, config['role_reviewer']['token'])
        assert pr.state == 'approved'

    @pytest.mark.parametrize('indent', ['', '\N{SPACE}', '\N{SPACE}'*4, '\N{TAB}'])
    def test_botname_indented(self, repo, env, indent, config):
        """ matching botname should ignore leading whitespaces
        """
        with repo:
            m, c = repo.make_commits(
                None,
                Commit('initial', tree={'m': 'm'}),
                Commit('first', tree={'m': 'c'}),
            )
            repo.make_ref('heads/master', m)
            prx = repo.make_pr(title='title', target='master', head=c)

        pr = to_pr(env, prx)
        assert pr.state == 'opened'

        with repo:
            prx.post_comment(f'{indent}hansen r+', config['role_reviewer']['token'])
        assert pr.state == 'approved'

    def test_unknown_commands(self, repo, env, config, users):
        with repo:
            [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

            [c] = repo.make_commits(m, Commit('first', tree={'m': 'c'}))
            pr = repo.make_pr(title='title', body=None, target='master', head=c)
            pr.post_comment("hansen do the thing", config['role_reviewer']['token'])
            pr.post_comment('hansen @bobby-b r+ :+1:', config['role_reviewer']['token'])
        env.run_crons()

        assert pr.comments == [
            (users['reviewer'], "hansen do the thing"),
            (users['reviewer'], "hansen @bobby-b r+ :+1:"),
            seen(env, pr, users),
            (users['user'], """\
@{reviewer} unknown command 'do'.

For your own safety I've ignored *everything in your entire comment*.

Currently available commands:

|command||
|-|-|
|`help`|displays this help|
|`r(eview)+`|approves the PR, if it&#39;s a forwardport also approves all non-detached parents|
|`r(eview)=<number>`|only approves the specified parents|
|`r(eview)-`|removes approval of a previously approved PR, if the PR is staged the staging will be cancelled|
|`retry`|re-tries staging a PR in the &#34;error&#34; state|
|`fw=no`|does not forward-port this PR|
|`fw=default`|forward-ports this PR normally|
|`fw=skipci`|does not wait for a forward-port&#39;s statuses to succeed before creating the next one|
|`fw=skipmerge`|does not wait for the source to be merged before creating forward ports|
|`up to <branch>`|only ports this PR forward to the specified branch (included)|
|`merge`|integrate the PR with a simple merge commit, using the PR description as message|
|`rebase-merge`|rebases the PR on top of the target branch the integrates with a merge commit, using the PR description as message|
|`rebase-ff`|rebases the PR on top of the target branch, then fast-forwards|
|`squash`|squashes the PR as a single commit on the target branch, using the PR description as message|
|`delegate+`|grants approval rights to the PR author|
|`delegate=<...>`|grants approval rights on this PR to the specified github users|
|`nice`|only stages the PR if there&#39;s room in the batch after `default` PRs|
|`default`|stages the PR normally|
|`priority`|tries to stage this PR first, then adds `default` PRs if the staging has room|
|`alone`|stages this PR only with other PRs of the same priority|
|`cancel=staging`|automatically cancels the current staging when this PR becomes ready|
|`check`|fetches or refreshes PR metadata, resets mergebot state|
|`remindme:<branch>=<message>`|When the PR gets forward-ported to &lt;branch&gt;, ping you with &lt;message&gt;. &lt;message&gt; can be quoted if it needs spaces.|

Note: this help text is dynamic and will change with the state of the PR.
""".format_map(users)),
            (users['user'], """\
@{reviewer} unknown command '@bobby-b'.

For your own safety I've ignored *everything in your entire comment*.

Currently available commands:

|command||
|-|-|
|`help`|displays this help|
|`r(eview)+`|approves the PR, if it&#39;s a forwardport also approves all non-detached parents|
|`r(eview)=<number>`|only approves the specified parents|
|`r(eview)-`|removes approval of a previously approved PR, if the PR is staged the staging will be cancelled|
|`retry`|re-tries staging a PR in the &#34;error&#34; state|
|`fw=no`|does not forward-port this PR|
|`fw=default`|forward-ports this PR normally|
|`fw=skipci`|does not wait for a forward-port&#39;s statuses to succeed before creating the next one|
|`fw=skipmerge`|does not wait for the source to be merged before creating forward ports|
|`up to <branch>`|only ports this PR forward to the specified branch (included)|
|`merge`|integrate the PR with a simple merge commit, using the PR description as message|
|`rebase-merge`|rebases the PR on top of the target branch the integrates with a merge commit, using the PR description as message|
|`rebase-ff`|rebases the PR on top of the target branch, then fast-forwards|
|`squash`|squashes the PR as a single commit on the target branch, using the PR description as message|
|`delegate+`|grants approval rights to the PR author|
|`delegate=<...>`|grants approval rights on this PR to the specified github users|
|`nice`|only stages the PR if there&#39;s room in the batch after `default` PRs|
|`default`|stages the PR normally|
|`priority`|tries to stage this PR first, then adds `default` PRs if the staging has room|
|`alone`|stages this PR only with other PRs of the same priority|
|`cancel=staging`|automatically cancels the current staging when this PR becomes ready|
|`check`|fetches or refreshes PR metadata, resets mergebot state|
|`remindme:<branch>=<message>`|When the PR gets forward-ported to &lt;branch&gt;, ping you with &lt;message&gt;. &lt;message&gt; can be quoted if it needs spaces.|

Note: this help text is dynamic and will change with the state of the PR.
""".format_map(users)),
        ]

class TestRMinus:
    def test_rminus_approved(self, repo, env, config):
        """ approved -> r- -> opened
        """
        with repo:
            [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

            [c] = repo.make_commits(m, Commit('first', tree={'m': 'c'}))
            prx = repo.make_pr(title='title', body=None, target='master', head=c)

        pr = to_pr(env, prx)
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
            [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

            [c] = repo.make_commits(m, Commit('first', tree={'m': 'c'}))
            prx = repo.make_pr(title='title', body=None, target='master', head=c)
            repo.post_status(prx.head, 'success')
        env.run_crons()

        pr = to_pr(env, prx)
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
            [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

            [c] = repo.make_commits(m, Commit('first', tree={'m': 'c'}))
            prx = repo.make_pr(title='title', body=None, target='master', head=c)
            repo.post_status(prx.head, 'success')
        env.run_crons()

        pr = to_pr(env, prx)

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
            [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

            repo.make_commits(m, Commit('first', tree={'1': '1'}), ref='heads/p1')
            prx1 = repo.make_pr(title='t1', body='b1', target='master', head='p1')
            repo.post_status(prx1.head, 'success')
            prx1.post_comment('hansen r+', config['role_reviewer']['token'])

            repo.make_commits(m, Commit('first', tree={'2': '2'}), ref='heads/p2')
            prx2 = repo.make_pr(title='t2', body='b2', target='master', head='p2')
            repo.post_status(prx2.head, 'success')
            prx2.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        pr1, pr2 = env['runbot_merge.pull_requests'].search([], order='number')
        assert pr1.number == prx1.number
        assert pr2.number == prx2.number
        assert pr1.staging_id == pr2.staging_id
        s0 = pr1.staging_id

        with repo:
            repo.post_status('staging.master', 'failure')
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

class TestComments:
    def test_address_method(self, repo, env, config):
        with repo:
            [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)

            repo.post_status(prx.head, 'success')
            prx.post_comment('hansen delegate=foo', config['role_reviewer']['token'])
            prx.post_comment('@hansen delegate=bar', config['role_reviewer']['token'])
            prx.post_comment('#hansen delegate=baz', config['role_reviewer']['token'])

        pr = to_pr(env, prx)

        assert {p.github_login for p in pr.delegates} \
            == {'foo', 'bar', 'baz'}

    def test_delete(self, repo, env, config):
        """ Comments being deleted should be ignored
        """
        with repo:
            [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
        pr = to_pr(env, prx)

        with repo:
            cid = prx.post_comment('hansen r+', config['role_reviewer']['token'])
            # unreview by pushing a new commit
            repo.make_commits(c1, Commit('second', tree={'m': 'c2'}), ref=prx.ref, make=False)
        assert pr.state == 'opened'
        with repo:
            prx.delete_comment(cid, config['role_reviewer']['token'])
        # check that PR is still unreviewed
        assert pr.state == 'opened'

    def test_edit(self, repo, env, config):
        """ Comments being edited should be ignored
        """
        with repo:
            [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
        pr = to_pr(env, prx)

        with repo:
            cid = prx.post_comment('hansen r+', config['role_reviewer']['token'])
            # unreview by pushing a new commit
            repo.make_commits(c1, Commit('second', tree={'m': 'c2'}), ref=prx.ref, make=False)
        assert pr.state == 'opened'
        with repo:
            prx.edit_comment(cid, 'hansen r+ edited', config['role_reviewer']['token'])
        # check that PR is still unreviewed
        assert pr.state == 'opened'

class TestFeedback:
    def test_ci_approved(self, repo, env, users, config, project):
        """CI failing on an r+'d PR sends feedback"""
        project.repo_ids.required_statuses = 'legal/cla,ci/runbot'
        with repo:
            [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref="heads/master")

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            pr = repo.make_pr(title='title', body='body', target='master', head=c1)
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
        env.run_crons()

        pr_id = to_pr(env, pr)
        assert pr_id.state == 'approved'

        for ctx, url in [
            ('ci/runbot', 'https://a'),
            ('ci/runbot', 'https://a'),
            ('legal/cla', 'https://b'),
            ('foo/bar', 'https://c'),
            ('ci/runbot', 'https://a'),
            ('legal/cla', 'https://d'),  # url changes so different from the previous
        ]:
            with repo:
                repo.post_status(pr_id.head, 'failure', ctx, target_url=url)
            env.run_crons()

        assert pr.comments == [
            (users['reviewer'], 'hansen r+'),
            seen(env, pr, users),
            (users['user'], "@{user} @{reviewer} 'ci/runbot' failed on this reviewed PR.".format_map(users)),
            (users['user'], "@{user} @{reviewer} 'legal/cla' failed on this reviewed PR.".format_map(users)),
            (users['user'], "@{user} @{reviewer} 'legal/cla' failed on this reviewed PR.".format_map(users)),
        ]

    def test_review_failed(self, repo, env, users, config):
        """r+-ing a PR with failed CI sends feedback"""
        with repo:
            [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

            [c1] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}))
            prx = repo.make_pr(title='title', body='body', target='master', head=c1)
        pr = to_pr(env, prx)

        with repo:
            repo.post_status(prx.head, 'failure')
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
            m0, m1 = repo.make_commits(
                None,
                Commit('initial', tree={'m': 'm0'}),
                Commit('first', tree={'m': 'm1'}),
                ref='heads/master',
            )
            repo.protect('master')

            [c1] = repo.make_commits(m0, Commit('other', tree={'m': 'c1'}))
            with pytest.raises(AssertionError):
                repo.update_ref('heads/master', c1, force=True)
        assert repo.get_ref('heads/master') == m1

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
