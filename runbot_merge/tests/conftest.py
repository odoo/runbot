import pytest


@pytest.fixture(params=['pat', 'app'])
def project(env, config, rolemap, request, mb_app, installation):
    match request.param:
        case 'pat':
            github_id = env['runbot_merge.github_connector'].create({
                'name': "PAT",
                'method': 'pat',
                'main_pat': config['github']['token'],
            }).id
        case 'app':
            github_id = env["runbot_merge.github_connector"].create({
                'name': "App",
                "method": "app",
                "app_id": mb_app.id,
                "client_id": mb_app.client_id,
                "client_secret": mb_app.client_secret,
                "private_key": mb_app.pem,
                "webhook_secret": mb_app.webhook_secret,
                "installations": [(0, 0, {
                    "installation_id": installation.id,
                    "owner_id": installation.account.id,
                    "owner_login": installation.account.login,
                    "repository_selection": "all",
                })],
            }).id
        case method:
            raise ValueError(f"Unknown auth method {method!r}")

    return env['runbot_merge.project'].create({
        'name': 'odoo',
        'github_id': github_id,
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
        repo_hooks = project.github_id.method == 'pat'
        r = make_repo(name, hooks=repo_hooks)
        rr = env['runbot_merge.repository'].create({
            'project_id': project.id,
            'name': r.name,
            'group_id': False,
            'required_statuses': 'default',
        })
        setreviewers(rr)
        if repo_hooks:
            env['runbot_merge.events_sources'].create({'repository': r.name})
        return r
    return mr


@pytest.fixture
def repo(make_repo2):
    return make_repo2('repo')
