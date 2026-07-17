import json

import pytest

from utils import Commit, to_pr, seen


def test_optional_statuses(env, project, make_repo, users, setreviewers, config):
    repository = make_repo('repo')
    env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repository.name,
        'status_ids': [(0, 0, {'context': 'l/int', 'prs': 'optional'})]
    })
    setreviewers(*project.repo_ids)
    env['runbot_merge.events_sources'].create({'repository': repository.name, 'secret': config['github']['secret']})

    with repository:
        m = repository.make_commits(None, Commit('root', tree={'a': '1'}), ref='heads/master')

        repository.make_commits(m, Commit('pr', tree={'a': '2'}), ref='heads/change')
        pr = repository.make_pr(target='master', title='super change', head='change')
    env.run_crons()

    # if an optional status is never received then the PR is valid
    pr_id = to_pr(env, pr)
    assert pr_id.state == 'validated'

    # If a run has started, then the PR is pending (not considered valid), this
    # limits the odds of merging a PR even though it's not valid, as long as the
    # optional status starts running before all the required statuses arrive
    # (with a success result).
    with repository:
        repository.post_status(pr.head, 'pending', 'l/int')
    env.run_crons()
    assert pr_id.state == 'opened'

    # If the status fails, then the PR is rejected.
    with repository:
        repository.post_status(pr.head, 'failure', 'l/int')
    env.run_crons()
    assert pr_id.state == 'opened'

    # re-run the job / fix the PR
    with repository:
        repository.post_status(pr.head, 'pending', 'l/int')
    env.run_crons()
    assert pr_id.state == 'opened'

    with repository:
        repository.post_status(pr.head, 'success', 'l/int')
    env.run_crons()
    assert pr_id.state == 'validated'

def test_incomplete_statuses_request(env, project, make_repo, users, setreviewers, config):
    """If "request missing statuses" is set (configured?) and a PR does not
    have at least a `pending` for every required status, send a request.
    """
    project.request_missing_statuses = True
    repo = make_repo('repo')
    env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'status_ids': [(0, 0, {'context': 'l/azy'})]
    })
    setreviewers(*project.repo_ids)
    env['runbot_merge.events_sources'].create({'repository': repo.name, 'secret': config['github']['secret']})

    with repo:
        m = repo.make_commits(None, Commit('root', tree={'a': '1'}), ref='heads/master')

        [c] = repo.make_commits(m, Commit('pr', tree={'a': '2'}), ref='heads/change')
        pr = repo.make_pr(target='master', title='super change', head=c)
    env.run_crons()

    assert env['saas.calls'].search([]) == env['saas.calls']

    with repo:
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    assert env['saas.calls'].search_read([], ['method', 'url', 'body']) == [{
        'id': 1,
        'method': 'POST',
        'url': 'https://runbot.odoo.com/runbot/request_ci',
        'body': json.dumps({"pull_requests": [f"{repo.name}#{pr.number}"]})
    }]

@pytest.mark.parametrize('status', [
    'success', 'failure', 'pending', 'error',
])
def test_complete_statuses_norequest(
    env, project, make_repo, users, setreviewers, config, status
, botname):
    """If *any* status has been sent on the status we don't trigger the
    request.
    """
    project.request_missing_statuses = True
    repo = make_repo('repo')
    env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': repo.name,
        'status_ids': [(0, 0, {'context': 'l/azy'}), (0, 0, {'context': 'o/ptional', 'prs': 'optional'})]
    })
    setreviewers(*project.repo_ids)
    env['runbot_merge.events_sources'].create({'repository': repo.name, 'secret': config['github']['secret']})

    with repo:
        m = repo.make_commits(None, Commit('root', tree={'a': '1'}), ref='heads/master')

        [c] = repo.make_commits(m, Commit('pr', tree={'a': '2'}), ref='heads/change')
        pr = repo.make_pr(target='master', title='super change', head=c)
        repo.post_status(c, status, 'l/azy')
    env.run_crons()

    with repo:
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    assert env['saas.calls'].search([]) == env['saas.calls']
    if status not in ('error', 'failure'):
        assert pr.comments == [
            (botname, seen(env, pr)),
            (users['reviewer'], 'hansen r+'),
        ]
    else:
        assert pr.comments == [
            (botname, seen(env, pr)),
            (users['reviewer'], 'hansen r+'),
            (botname, "@{reviewer} you may want to rebuild or fix this PR as it has failed CI.".format_map(users)),
        ]