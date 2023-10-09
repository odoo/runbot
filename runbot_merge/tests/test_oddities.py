import requests

from utils import Commit, to_pr


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
    pr0, pr1, pr2 = prs.name_get()

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
    p = env['res.partner'].create({
        'name': 'George Pearce',
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

    assert p.review_rights == env['res.partner.review']

def test_staging_post_update(env, project, make_repo, setreviewers, users, config):
    """Because statuses come from commits, it's possible to update the commits
    of a staging after that staging has completed (one way or the other), either
    by sending statuses directly (e.g. rebuilding, for non-deterministic errors)
    or just using the staging's head commit in a branch.

    This makes post-mortem analysis quite confusing, so stagings should
    "lock in" their statuses once they complete.
    """
    repo = make_repo('repo')
    project.write({'repo_ids': [(0, 0, {
        'name': repo.name,
        'group_id': False,
        'required_statuses': 'legal/cla,ci/runbot'
    })]})
    setreviewers(*project.repo_ids)

    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

        repo.make_commits(m, Commit('thing', tree={'m': 'c'}), ref='heads/other')
        pr = repo.make_pr(target='master', head='other')
        repo.post_status(pr.head, 'success', 'ci/runbot')
        repo.post_status(pr.head, 'success', 'legal/cla')
        pr.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()
    pr_id = to_pr(env, pr)
    staging_id = pr_id.staging_id
    assert staging_id

    staging_head = repo.commit('staging.master')
    with repo:
        repo.post_status(staging_head, 'failure', 'ci/runbot')
    env.run_crons()
    assert pr_id.state == 'error'
    assert staging_id.state == 'failure'
    assert staging_id.statuses == [
        [repo.name, 'ci/runbot', 'failure', ''],
    ]

    with repo:
        repo.post_status(staging_head, 'success', 'ci/runbot')
    env.run_crons()
    assert staging_id.state == 'failure'
    assert staging_id.statuses == [
        [repo.name, 'ci/runbot', 'failure', ''],
    ]

def test_merge_empty_commits(env, project, make_repo, setreviewers, users, config):
    """The mergebot should allow merging already-empty commits.
    """
    repo = make_repo('repo')
    project.write({'repo_ids': [(0, 0, {
        'name': repo.name,
        'group_id': False,
        'required_statuses': 'default',
    })]})
    setreviewers(*project.repo_ids)

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

def test_merge_emptying_commits(env, project, make_repo, setreviewers, users, config):
    """The mergebot should *not* allow merging non-empty commits which become
    empty as part of the staging (rebasing)
    """
    repo = make_repo('repo')
    project.write({'repo_ids': [(0, 0, {
        'name': repo.name,
        'group_id': False,
        'required_statuses': 'default',
    })]})
    setreviewers(*project.repo_ids)

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
    assert not to_pr(env, pr1).staging_id
    assert pr1.comments[3:] == [
        (users['user'], f"{ping} unable to stage: commit {c1} results in an empty tree when merged, it is likely a duplicate of a merged commit, rebase and remove.")
    ]

    # check that followup commit emptying is caught
    assert not to_pr(env, pr2).staging_id
    assert pr2.comments[3:] == [
        (users['user'], f"{ping} unable to stage: commit {c2} results in an empty tree when merged, it is likely a duplicate of a merged commit, rebase and remove.")
    ]

    # check that emptied squashed pr is caught
    pr3_id = to_pr(env, pr3)
    assert not pr3_id.staging_id
    assert pr3.comments[3:] == [
        (users['user'], f"{ping} unable to stage: results in an empty tree when merged, might be the duplicate of a merged PR.")
    ]
