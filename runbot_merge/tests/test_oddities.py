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
