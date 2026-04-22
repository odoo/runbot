import datetime

import pytest
import requests

from utils import Commit, to_pr, seen, read_tracking_value, matches


def test_partner_merge(env):
    p_src = env['res.partner'].create({
        'name': "xxx",
        'github_login': 'xxx'
    })
    # proper login with useful info
    p_dest = env['res.partner'].create({
        'name': 'Partner P. Partnersson',
        'github_login': ''
    })

    env['base.partner.merge.automatic.wizard'].create({
        'state': 'selection',
        'partner_ids': (p_src + p_dest).ids,
        'dst_partner_id': p_dest.id,
    }).action_merge()
    assert not p_src.exists()
    assert p_dest.name == 'Partner P. Partnersson'
    assert p_dest.github_login == 'xxx'

def test_name_search(env):
    """ PRs should be findable by:

    * number
    * display_name (`repository#number`)
    * label

    This way we can find parents or sources by these informations.
    """
    p = env['runbot_merge.project'].create({
        'name': 'proj',
        'github_token': 'no',
        'github_name': "noo",
        'github_email': "nooo@example.org",
    })
    b = env['runbot_merge.branch'].create({
        'name': 'target',
        'project_id': p.id
    })
    r = env['runbot_merge.repository'].create({
        'name': 'repo',
        'project_id': p.id,
    })

    baseline = {'target': b.id, 'repository': r.id}
    PRs = env['runbot_merge.pull_requests']
    prs = PRs.create({**baseline, 'number': 1964, 'label': 'victor:thump', 'head': 'a', 'message': 'x'})\
        | PRs.create({**baseline, 'number': 1959, 'label': 'marcus:frankenstein', 'head': 'b', 'message': 'y'})\
        | PRs.create({**baseline, 'number': 1969, 'label': 'victor:patch-1', 'head': 'c', 'message': 'z'})
    pr0, pr1, pr2 = [[pr.id, pr.display_name] for pr in prs]

    assert PRs.name_search('1964') == [pr0]
    assert PRs.name_search('1969') == [pr2]

    assert PRs.name_search('frank') == [pr1]
    assert PRs.name_search('victor') == [pr2, pr0]

    assert PRs.name_search('thump') == [pr0]

    assert PRs.name_search('repo') == [pr2, pr0, pr1]
    assert PRs.name_search('repo#1959') == [pr1]

def test_unreviewer(env, project, port):
    repo = env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': 'a_test_repo',
        'status_ids': [(0, 0, {'context': 'status'})]
    })
    p = env['res.users'].create({
        'name': 'George Pearce',
        'login': 'pewpew70',
        'github_login': 'emubitch',
        'review_rights': [(0, 0, {'repository_id': repo.id, 'review': True})]
    })

    r = requests.post(f'http://localhost:{port}/runbot_merge/get_reviewers', json={
        'jsonrpc': '2.0',
        'id': None,
        'method': 'call',
        'params': {},
    })
    r.raise_for_status()
    assert 'error' not in r.json()
    assert r.json()['result'] == ['emubitch']

    r = requests.post(f'http://localhost:{port}/runbot_merge/remove_reviewers', json={
        'jsonrpc': '2.0',
        'id': None,
        'method': 'call',
        'params': {'github_logins': ['emubitch']},
    })
    r.raise_for_status()
    assert 'error' not in r.json()

    assert not p.active
    assert not p.email
    assert p.review_rights == env['res.partner.review']

def test_staging_post_update(env, repo, users, config):
    """Because statuses come from commits, it's possible to update the commits
    of a staging after that staging has completed (one way or the other), either
    by sending statuses directly (e.g. rebuilding, for non-deterministic errors)
    or just using the staging's head commit in a branch.

    This makes post-mortem analysis quite confusing, so stagings should
    "lock in" their statuses once they complete.
    """

    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

        repo.make_commits(m, Commit('thing', tree={'m': 'c'}), ref='heads/other')
        pr = repo.make_pr(target='master', head='other')
        repo.post_status(pr.head, 'success')
        pr.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()
    pr_id = to_pr(env, pr)
    staging_id = pr_id.staging_id
    assert staging_id

    staging_head = repo.commit('staging.master')
    with repo:
        repo.post_status(staging_head, 'failure')
    env.run_crons()
    assert pr_id.state == 'error'
    assert staging_id.state == 'failure'
    assert staging_id.statuses == [
        [repo.name, 'default', 'failure', ''],
    ]

    with repo:
        repo.post_status(staging_head, 'success')
    env.run_crons()
    assert staging_id.state == 'failure'
    assert staging_id.statuses == [
        [repo.name, 'default', 'failure', ''],
    ]

def test_merge_empty_commits(env, repo, users, config):
    """The mergebot should allow merging already-empty commits.
    """
    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

        repo.make_commits(m, Commit('thing1', tree={}), ref='heads/other1')
        pr1 = repo.make_pr(target='master', head='other1')
        repo.post_status(pr1.head, 'success')
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])

        repo.make_commits(m, Commit('thing2', tree={}), ref='heads/other2')
        pr2 = repo.make_pr(target='master', head='other2')
        repo.post_status(pr2.head, 'success')
        pr2.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])
    env.run_crons()
    pr1_id = to_pr(env, pr1)
    pr2_id = to_pr(env, pr2)
    assert pr1_id.staging_id and pr2_id.staging_id

    with repo:
        repo.post_status('staging.master', 'success')
    env.run_crons()

    assert pr1_id.state == pr2_id.state == 'merged'

    # log is most-recent-first (?)
    commits = list(repo.log('master'))
    head = repo.commit(commits[0]['sha'])
    assert repo.read_tree(head) == {'m': 'm'}

    assert commits[0]['commit']['message'].startswith('thing2')
    assert commits[1]['commit']['message'].startswith('thing1')
    assert commits[2]['commit']['message'] == 'initial'


def test_merge_emptying_commits(env, repo, users, config):
    """The mergebot should *not* allow merging non-empty commits which become
    empty as part of the staging (rebasing)
    """
    with repo:
        [m, _] = repo.make_commits(
            None,
            Commit('initial', tree={'m': 'm'}),
            Commit('second', tree={'m': 'c'}),
            ref='heads/master',
        )

        [c1] = repo.make_commits(m, Commit('thing', tree={'m': 'c'}), ref='heads/branch1')
        pr1 = repo.make_pr(target='master', head='branch1')
        repo.post_status(pr1.head, 'success')
        pr1.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])

        [_, c2] = repo.make_commits(
            m,
            Commit('thing1', tree={'c': 'c'}),
            Commit('thing2', tree={'m': 'c'}),
            ref='heads/branch2',
        )
        pr2 = repo.make_pr(target='master', head='branch2')
        repo.post_status(pr2.head, 'success')
        pr2.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])

        repo.make_commits(
            m,
            Commit('thing1', tree={'m': 'x'}),
            Commit('thing2', tree={'m': 'c'}),
            ref='heads/branch3',
        )
        pr3 = repo.make_pr(target='master', head='branch3')
        repo.post_status(pr3.head, 'success')
        pr3.post_comment('hansen r+ squash', config['role_reviewer']['token'])
    env.run_crons()

    ping = f"@{users['user']} @{users['reviewer']}"
    # check that first / sole commit emptying is caught
    pr1_id = to_pr(env, pr1)
    assert not pr1_id.staging_id
    assert pr1.comments[3:] == [
        (users['user'], f"{ping} unable to stage: commit {c1} results in an empty tree when merged, it is likely a duplicate of a merged commit, rebase and remove.")
    ]
    assert pr1_id.error
    assert pr1_id.state == 'error'

    # check that followup commit emptying is caught
    pr2_id = to_pr(env, pr2)
    assert not pr2_id.staging_id
    assert pr2.comments[3:] == [
        (users['user'], f"{ping} unable to stage: commit {c2} results in an empty tree when merged, it is likely a duplicate of a merged commit, rebase and remove.")
    ]
    assert pr2_id.error
    assert pr2_id.state == 'error'

    # check that emptied squashed pr is caught
    pr3_id = to_pr(env, pr3)
    assert not pr3_id.staging_id
    assert pr3.comments[3:] == [
        (users['user'], f"{ping} unable to stage: results in an empty tree when merged, might be the duplicate of a merged PR.")
    ]
    assert pr3_id.error
    assert pr3_id.state == 'error'

    # ensure the PR does not get re-staged since it's the first of the staging
    # (it's the only one)
    env.run_crons()
    assert pr1.comments[3:] == [
        (users['user'], f"{ping} unable to stage: commit {c1} results in an empty tree when merged, it is likely a duplicate of a merged commit, rebase and remove.")
    ]
    assert len(pr2.comments) == 4
    assert len(pr3.comments) == 4

def test_force_ready(env, repo, config):
    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref="heads/master")

        repo.make_commits(m, Commit('first', tree={'m': 'c1'}), ref="heads/other")
        pr = repo.make_pr(target='master', head='other')
    env.run_crons()

    pr_id = to_pr(env, pr)
    pr_id.skipchecks = True

    assert pr_id.state == 'ready'
    assert pr_id.status == 'success'
    reviewer = env['res.users'].browse([env._uid]).partner_id
    assert pr_id.reviewed_by == reviewer

def test_help(env, repo, config, users, partners):
    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref="heads/master")

        repo.make_commits(m, Commit('first', tree={'m': 'c1'}), ref="heads/other")
        pr = repo.make_pr(target='master', head='other')
    env.run_crons()

    for role in ['reviewer', 'self_reviewer', 'user', 'other']:
        v = config[f'role_{role}']
        with repo:
            pr.post_comment("hansen help", v['token'])
    with repo:
        pr.post_comment("hansen r+ help", config['role_reviewer']['token'])

    assert not partners['reviewer'].user_ids, "the reviewer should not be an internal user"

    group_internal = env.ref("base.group_user")
    group_admin = env.ref("runbot_merge.group_admin")
    env['res.users'].create({
        'partner_id': partners['reviewer'].id,
        'login': 'reviewer',
        'groups_id': [(4, group_internal.id, 0), (4, group_admin.id, 0)],
    })

    with repo:
        pr.post_comment("hansen help", config['role_reviewer']['token'])
    env.run_crons()

    assert pr.comments == [
        seen(env, pr, users),
        (users['reviewer'], "hansen help"),
        (users['self_reviewer'], "hansen help"),
        (users['user'], "hansen help"),
        (users['other'], "hansen help"),
        (users['reviewer'], "hansen r+ help"),
        (users['reviewer'], "hansen help"),
        (users['user'], REVIEWER.format(user=users['reviewer'], skip="", reset="")),
        (users['user'], RANDO.format(user=users['self_reviewer'])),
        (users['user'], AUTHOR.format(user=users['user'])),
        (users['user'], RANDO.format(user=users['other'])),
        (users['user'],
         REVIEWER.format(user=users['reviewer'], skip='', reset='')
         + "\n\nWarning: in invoking help, every other command has been ignored."),
        (users['user'], REVIEWER.format(
            user=users['reviewer'],
            skip='|`skipchecks`|bypasses both statuses and review|\n',
            reset="""\
|`reset=auto`|deletes splits and cancels staging if it didn't run too long|
|`reset=splits`|deletes splits|
|`reset=staging`|deletes splits and cancels staging unconditionally|
"""
        )),
    ]

REVIEWER = """\
Currently available commands for @{user}:

|command||
|-|-|
|`help`|displays this help|
|`r(eview)+`|approves the PR, if it's a forwardport also approves all non-detached parents|
|`r(eview)=<number>`|only approves the specified parents|
|`r(eview)-`|removes approval of a previously approved PR, if the PR is staged the staging will be cancelled|
|`retry`|re-tries staging a PR in the "error" state|
|`fw=no`|does not forward-port this PR|
|`fw=default`|forward-ports this PR normally|
|`fw=skipci`|does not wait for a forward-port's statuses to succeed before creating the next one|
|`fw=skipmerge`|does not wait for the source to be merged before creating forward ports|
|`up to <branch>`|only ports this PR forward to the specified branch (included)|
|`merge`|integrate the PR with a simple merge commit, using the PR description as message|
|`rebase-merge`|rebases the PR on top of the target branch the integrates with a merge commit, using the PR description as message|
|`rebase-ff`|rebases the PR on top of the target branch, then fast-forwards|
|`squash`|squashes the PR as a single commit on the target branch, using the PR description as message|
|`delegate+`|grants approval rights to the PR author|
|`delegate=<...>`|grants approval rights on this PR to the specified github users|
{reset}\
|`nice`|only stages the PR if there's room in the batch after `default` PRs|
|`default`|stages the PR normally|
|`priority`|tries to stage this PR first, then adds `default` PRs if the staging has room|
|`alone`|stages this PR only with other PRs of the same priority|
{skip}\
|`cancel=staging`|automatically cancels the current staging when this PR becomes ready|
|`check`|fetches or refreshes PR metadata, resets mergebot state|
|`remindme:<branch>=<message>`|When the PR gets forward-ported to <branch>, ping you with <message>. <message> can be quoted if it needs spaces.|

Note: this help text is dynamic and will change with the state of the PR.\
"""
AUTHOR = """\
Currently available commands for @{user}:

|command||
|-|-|
|`help`|displays this help|
|`r(eview)-`|removes approval of a previously approved PR, if the PR is staged the staging will be cancelled|
|`retry`|re-tries staging a PR in the "error" state|
|`fw=no`|does not forward-port this PR|
|`fw=default`|forward-ports this PR normally|
|`up to <branch>`|only ports this PR forward to the specified branch (included)|
|`check`|fetches or refreshes PR metadata, resets mergebot state|
|`remindme:<branch>=<message>`|When the PR gets forward-ported to <branch>, ping you with <message>. <message> can be quoted if it needs spaces.|

Note: this help text is dynamic and will change with the state of the PR.\
"""
RANDO = """\
Currently available commands for @{user}:

|command||
|-|-|
|`help`|displays this help|
|`remindme:<branch>=<message>`|When the PR gets forward-ported to <branch>, ping you with <message>. <message> can be quoted if it needs spaces.|

Note: this help text is dynamic and will change with the state of the PR.\
"""

@pytest.mark.parametrize("target", ["master", "other"])
def test_close_linked_issues(env, project, repo, config, users, partners, target):
    """Github's linked issues thingie only triggers when:

    - the commit with the reference reaches the default branch
    - the PR linked to the issue (via the UI or the PR description) is targeted
      at and merged into the default branch

    The former does eventually happen with odoo, after a while, usually:
    forward-ports will generally go through the default branch eventually amd
    the master becomes the default branch on the next major release.

    *However* the latter case basically doesn't happen, if a PR is merged into
    master it never "reaches the default branch", and while the description is
    ported forwards (with any link it contains) that's not the case of manual
    links (it's not even possible since there is no API to manipulate those).

    Thus support for linked issues needs to be added to the mergebot. Since the
    necessarily has write access to PRs (to close them on merge) it should have
    the same on issues.
    """
    project.write({'branch_ids': [(0, 0, {'name': 'other'})]})
    with repo:
        i1 = repo.make_issue(f"Issue 1")
        i2 = repo.make_issue(f"Issue 2")

        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref="heads/master")
        # non-default branch
        repo.make_ref("heads/other", m)
    # ensure the default branch is master so we have consistent testing state
    r = repo._session.patch(f'https://api.github.com/repos/{repo.name}', json={'default_branch': 'master'})
    assert r.ok, r.text

    with repo:
        # there are only two locations relevant to us:
        #
        # - commit message
        # - pr description
        #
        # the other two are manually linked issues (there's no API for that so
        # we can't test it) and the merge message (which for us is the PR
        # message)
        repo.make_commits(m, Commit(f'This is my commit\n\nfixes #{i1.number}', tree={'m': 'c1'}), ref="heads/pr")
        pr = repo.make_pr(target=target, head='pr', title="a pr", body=f"fixes #{i2.number}")
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
        repo.post_status(pr.head, 'success')

    env.run_crons(None)

    pr_id = to_pr(env, pr)
    assert pr_id.state == 'ready'
    assert pr_id.staging_id

    assert i1.state == 'open'
    assert i2.state == 'open'
    with repo:
        repo.post_status(f'staging.{target}', 'success')
    env.run_crons(None)
    assert pr_id.state == 'merged'
    assert i1.state == 'closed'
    assert i2.state == 'closed'

def test_staging_push_blocked(env, project, repo, config, users):
    """ If even pushing to the staging branch fails, there's something very
    wrong with the repository's configuration, so disable staging (on that
    branch as it might be a branch-specific protection issue) and warn everyone.
    """

    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref="heads/master")

        [c] = repo.make_commits(m, Commit('first', tree={'m': 'c1'}), ref="heads/other")
        pr = repo.make_pr(target='master', head='other')
        repo.post_status(pr.head, 'success')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])

    r = repo._get_session(None).post(
        f"https://api.github.com/repos/{repo.name}/rulesets",
        json={
            "name": "Prevent push to repo",
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [],
            "conditions": {
                "ref_name": {
                    "include": ["~ALL"],
                    "exclude": [],
                }
            },
            "rules": [{"type": "creation"}, {"type": "update"}],
        }
    )
    assert r.ok, r.text

    env.run_crons()
    staging = env['runbot_merge.stagings'].search([('active', '=', False)])
    assert staging.state == 'failure'
    assert staging.reason == f'Failed pushing to {repo.name}'
    assert staging.message_ids[::-1].mapped(
        lambda m: m.body.strip() or list(map(read_tracking_value, m.tracking_value_ids))
    ) == [
        '<p>A set of batches being tested for integration created</p>',
        matches(f"<p>Staging on master has been disabled because pushing to {repo.name} failed:</p>"
        f"""\
<pre>\
remote: error: GH013: Repository rule violations found for refs/heads/staging.master.        
remote: Review all repository rules at https://github.com/{repo.name}/rules?ref=refs%2Fheads%2Fstaging.master        
remote: 
remote: - Cannot create ref due to creations being restricted.        
remote: 
To https://github.com/{repo.name}
 ! [remote rejected] $$ -&gt; staging.master (push declined due to repository rule violations)
error: failed to push some refs to 'https://github.com/{repo.name}'
</pre>\
"""),
        # not sure why the tracking values don't appear...
    ]
    assert project.branch_ids.staging_enabled is False

def test_outdated_pr(env, project, repo, config, users):
    """ If a PR is too old (in number of commits), skip staging it and send a
    message.
    """
    project.lateness_limit = 1  # "require branches to be up to date before merging"

    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref="heads/master")

        repo.make_commits(m, Commit('first', tree={'m1': 'c1'}), ref="heads/other1")
        repo.make_commits(m, Commit('first', tree={'m2': 'c2'}), ref="heads/other2")
        pr1 = repo.make_pr(target='master', head='other1')
        repo.post_status(pr1.head, 'success')
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])

        pr2 = repo.make_pr(target='master', head='other2')
        repo.post_status(pr2.head, 'success')
        pr2.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    pr1_id = to_pr(env, pr1)
    pr2_id = to_pr(env, pr2)
    assert pr1_id.staging_id
    assert not pr2_id.staging_id

    with repo:
        repo.post_status('staging.master', 'success')
    env.run_crons()

    assert pr1_id.state == 'merged'
    assert pr2_id.state == 'error'
    assert pr2.comments == [
        (users['reviewer'], "hansen r+"),
        seen(env, pr2, users),
        (users['user'], f"@{users['user']} @{users['reviewer']} unable to stage: too old (1 commits behind), please rebase."),
    ]

def test_not_outdated_pr(env, project, repo, config, users):
    """ If a PR is too old (in number of commits), skip staging it and send a
    message.
    """
    project.lateness_limit = 1  # "require branches to be up to date before merging"

    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref="heads/master")

        repo.make_commits(m, Commit('first', tree={'m1': 'c1'}), ref="heads/other1")
        repo.make_commits(m, Commit('first', tree={'m2': 'c2'}), ref="heads/other2")
        pr1 = repo.make_pr(target='master', head='other1')
        repo.post_status(pr1.head, 'success')
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])

        pr2 = repo.make_pr(target='master', head='other2')
        repo.post_status(pr2.head, 'success')
        pr2.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    pr1_id = to_pr(env, pr1)
    pr2_id = to_pr(env, pr2)
    assert pr1_id.staging_id
    assert not pr2_id.staging_id

    with repo:
        repo.post_status('staging.master', 'failure')
    env.run_crons()

    assert pr1_id.state == 'error'
    assert pr2_id.staging_id

def test_many_commits_is_not_late(env, project, repo, config, users):
    project.lateness_limit = 2  # "require branches to be up to date before merging"

    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref="heads/master")

        repo.make_commits(
            m,
            *(Commit(f'{i}', tree={'m1': f'c{i}'}) for i in range(100)),
            ref="heads/other1",
        )
        pr1 = repo.make_pr(target='master', head='other1')
        repo.post_status(pr1.head, 'success')
        pr1.post_comment('hansen r+ merge', config['role_reviewer']['token'])

        repo.make_commits(m, Commit('first', tree={'m2': 'c2'}), ref="heads/other2")
        pr2 = repo.make_pr(target='master', head='other2')
        repo.post_status(pr2.head, 'success')
        pr2.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    pr1_id = to_pr(env, pr1)
    pr2_id = to_pr(env, pr2)
    assert pr1_id.staging_id
    assert pr2_id.staging_id

@pytest.mark.expect_log_errors(
    reason="Trying to update a cron while running it fails with"
           " lock_not_available",
)
def test_cron_xaccess(env):
    """Because the run and the cron are in different transactions and the cron
    transaction has an exclusive lock on the cron, a cron run can not disable
    its cron.
    """
    cron = env['ir.cron'].create({
        'name': "my cron",
        'state': 'code',
        'model_id': env.ref('runbot_merge.model_runbot_merge_pull_requests_feedback').id,
        'numbercall': '-1',
    })
    cron.code = f"env['ir.cron'].browse({cron.id})['active'] = False"
    assert cron.active
    cron.trigger()
    env.run_crons()
    assert cron.active

@pytest.mark.parametrize('code,active', [
    ("env.context['deactivate'](True)", False),
    ("env.context['deactivate'](False)", True),
    ("pass", True),
])
def test_cron_autodisable(env, code, active):
    cron = env['ir.cron'].create({
        'name': "my cron",
        'state': 'code',
        'model_id': env.ref('runbot_merge.model_runbot_merge_pull_requests_feedback').id,
        'numbercall': '-1',
        'code': code,
    })
    assert cron.active
    cron.trigger()
    env.run_crons()
    assert cron.active == active

def test_empty_split(env, project, repo, users, config):
    b = env['runbot_merge.batch'].create({
        'target': project.branch_ids.id,
        'merge_date': datetime.datetime.now(),
    })
    st = env['runbot_merge.stagings'].create({
        'target': project.branch_ids.id,
        'active': False,
        'state': 'failure',
        'staging_end': datetime.datetime.now(),
        'staging_batch_ids': [(0, 0, {'runbot_merge_batch_id': b.id})]
    })
    env['runbot_merge.split'].create({
        'target': project.branch_ids.id,
        'staging_id': st.id,
        'batch_ids': [],
        'original_batches': [],
    })
    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

        repo.make_commits(m, Commit('thing1', tree={}), ref='heads/other1')
        pr1 = repo.make_pr(target='master', head='other1')
        repo.post_status(pr1.head, 'success')
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    assert to_pr(env, pr1).staging_id

def test_create_commits_count(
        port, env, project, repo, users, config,
):
    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

        [c] = repo.make_commits(m, Commit('thing1', tree={}), ref='heads/other1')
        with repo.disable_hooks():
            pr = repo.make_pr(target='master', head=c)
    env.run_crons()

    with pytest.raises(TimeoutError):
        to_pr(env, pr)

    r = requests.post(
        f"http://localhost:{port}/runbot_merge/hooks",
        headers={
            "X-Github-Event": "pull_request",
        },
        json={
            'action': 'opened',
            'sender': {'login': users['user']},
            'repository': {'full_name': repo.name},
            'pull_request': {
                'number': pr.number,
                'state': 'open',
                'user': {'login': users['user']},
                'head': {'sha': c, 'label': f'{repo.owner}:other1'},
                'base': {'ref': 'master', 'repo': {'full_name': repo.name}},
                'title': "c",
                'commits': 0,
                'draft': False,
            }
        }
    )
    r.raise_for_status()

    pr_id = to_pr(env, pr)
    assert not pr_id.squash
    env.run_crons()
    assert pr_id.squash

def test_sync_commits_count(port, env, project, repo, users, config) -> None:
    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

        [c] = repo.make_commits(m, Commit('thing1', tree={}), ref='heads/other1')
        pr = repo.make_pr(target='master', head=c)
    env.run_crons()

    # simulate github being stupid
    r = requests.post(
        f"http://localhost:{port}/runbot_merge/hooks",
        headers={
            "X-Github-Event": "pull_request",
        },
        json={
            'action': 'labeled',
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
                'commits': 0,
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

    pr_id = to_pr(env, pr)
    assert not pr_id.squash
    env.run_crons()
    assert pr_id.squash