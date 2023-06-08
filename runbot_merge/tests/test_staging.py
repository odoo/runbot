import pytest

from utils import Commit, to_pr


@pytest.fixture
def repo(env, project, make_repo, users, setreviewers):
    r = make_repo('repo')
    project.write({'repo_ids': [(0, 0, {
        'name': r.name,
        'group_id': False,
        'required_statuses': 'ci'
    })]})
    setreviewers(*project.repo_ids)
    return r

def test_staging_disabled_branch(env, project, repo, config):
    """Check that it's possible to disable staging on a specific branch
    """
    project.branch_ids = [(0, 0, {
        'name': 'other',
        'staging_enabled': False,
    })]
    with repo:
        [master_commit] = repo.make_commits(None, Commit("master", tree={'a': '1'}), ref="heads/master")
        [c1] = repo.make_commits(master_commit, Commit("thing", tree={'a': '2'}), ref='heads/master-thing')
        master_pr = repo.make_pr(title="whatever", target="master", head="master-thing")
        master_pr.post_comment("hansen r+", config['role_reviewer']['token'])
        repo.post_status(c1, 'success', 'ci')

        [other_commit] = repo.make_commits(None, Commit("other", tree={'b': '1'}), ref='heads/other')
        [c2] = repo.make_commits(other_commit, Commit("thing", tree={'b': '2'}), ref='heads/other-thing')
        other_pr = repo.make_pr(title="whatever", target="other", head="other-thing")
        other_pr.post_comment("hansen r+", config['role_reviewer']['token'])
        repo.post_status(c2, 'success', 'ci')
    env.run_crons()

    assert to_pr(env, master_pr).staging_id, \
        "master is allowed to stage, should be staged"
    assert not to_pr(env, other_pr).staging_id, \
        "other is *not* allowed to stage, should not be staged"
