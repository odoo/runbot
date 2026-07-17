import pytest


@pytest.fixture
def project(env, config, rolemap):
    return env['runbot_merge.project'].create({
        'name': 'odoo',
        'github_token': config['github']['token'],
        'github_prefix': 'hansen',
        'github_name': config['github']['name'],
        'github_email': "foo@example.org",
        'branch_ids': [(0, 0, {'name': 'master'})],
    })


@pytest.fixture
def make_repo2(env, project, make_repo, users, setreviewers, config):
    """Layer over ``make_repo`` which also:

    - adds the new repo to ``project`` (with no group and the ``'default'`` status required)
    - sets the standard reviewers on the repo
    - and creates an event source for the repo
    """
    def mr(name):
        r = make_repo(name)
        rr = env['runbot_merge.repository'].create({
            'project_id': project.id,
            'name': r.name,
            'group_id': False,
            'required_statuses': 'default',
        })
        setreviewers(rr)
        env['runbot_merge.events_sources'].create({'repository': r.name, 'secret': config['github']['secret']})
        return r
    return mr


@pytest.fixture
def repo(make_repo2):
    return make_repo2('repo')
