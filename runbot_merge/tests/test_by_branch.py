import pytest

from utils import Commit


@pytest.fixture
def repo(env, project, make_repo, users, setreviewers):
    r = make_repo('repo')
    project.write({
        'repo_ids': [(0, 0, {
            'name': r.name,
            'status_ids': [
                (0, 0, {'context': 'ci'}),
                # require the lint status on master
                (0, 0, {
                    'context': 'lint',
                    'branch_filter': [('id', '=', project.branch_ids.id)]
                }),
                (0, 0, {'context': 'pr', 'stagings': False}),
                (0, 0, {'context': 'staging', 'prs': False}),
            ]
        })],
    })
    setreviewers(*project.repo_ids)
    return r

def test_status_applies(env, repo, config):
    """ If branches are associated with a repo status, only those branch should
    require the status on their PRs & stagings
    """
    with repo:
        m = repo.make_commits(None, Commit('root', tree={'a': '1'}), ref='heads/master')

        [c] = repo.make_commits(m, Commit('pr', tree={'a': '2'}), ref='heads/change')
        pr = repo.make_pr(target='master', title="super change", head='change')
    pr_id = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', pr.number)
    ])
    assert pr_id.state == 'opened'

    with repo:
        repo.post_status(c, 'success', 'ci')
    env.run_crons('runbot_merge.process_updated_commits')
    assert pr_id.state == 'opened'
    with repo:
        repo.post_status(c, 'success', 'pr')
    env.run_crons('runbot_merge.process_updated_commits')
    assert pr_id.state == 'opened'
    with repo:
        repo.post_status(c, 'success', 'lint')
    env.run_crons('runbot_merge.process_updated_commits')
    assert pr_id.state == 'validated'

    with repo:
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    st = env['runbot_merge.stagings'].search([])
    assert st.state == 'pending'
    with repo:
        repo.post_status('staging.master', 'success', 'ci')
    env.run_crons('runbot_merge.process_updated_commits')
    assert st.state == 'pending'
    with repo:
        repo.post_status('staging.master', 'success', 'lint')
    env.run_crons('runbot_merge.process_updated_commits')
    assert st.state == 'pending'
    with repo:
        repo.post_status('staging.master', 'success', 'staging')
    env.run_crons('runbot_merge.process_updated_commits')
    assert st.state == 'success'

def test_status_skipped(env, project, repo, config):
    """ Branches not associated with a repo status should not require the status
    on their PRs or stagings
    """
    # add a second branch for which the lint status doesn't apply
    project.write({'branch_ids': [(0, 0, {'name': 'maintenance'})]})
    with repo:
        m = repo.make_commits(None, Commit('root', tree={'a': '1'}), ref='heads/maintenance')

        [c] = repo.make_commits(m, Commit('pr', tree={'a': '2'}), ref='heads/change')
        pr = repo.make_pr(target='maintenance', title="super change", head='change')
    pr_id = env['runbot_merge.pull_requests'].search([
        ('repository.name', '=', repo.name),
        ('number', '=', pr.number)
    ])
    assert pr_id.state == 'opened'

    with repo:
        repo.post_status(c, 'success', 'ci')
    env.run_crons('runbot_merge.process_updated_commits')
    assert pr_id.state == 'opened'
    with repo:
        repo.post_status(c, 'success', 'pr')
    env.run_crons('runbot_merge.process_updated_commits')
    assert pr_id.state == 'validated'

    with repo:
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    st = env['runbot_merge.stagings'].search([])
    assert st.state == 'pending'
    with repo:
        repo.post_status('staging.maintenance', 'success', 'staging')
    env.run_crons('runbot_merge.process_updated_commits')
    assert st.state == 'pending'
    with repo:
        repo.post_status('staging.maintenance', 'success', 'ci')
    env.run_crons('runbot_merge.process_updated_commits')
    assert st.state == 'success'
