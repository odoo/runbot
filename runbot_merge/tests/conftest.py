import pytest
import requests

@pytest.fixture()
def module():
    return 'runbot_merge'

@pytest.fixture
def page(port):
    s = requests.Session()
    def get(url):
        r = s.get('http://localhost:{}{}'.format(port, url))
        r.raise_for_status()
        return r.content
    return get

@pytest.fixture
def default_crons():
    return [
        # env['runbot_merge.project']._check_fetch()
        'runbot_merge.fetch_prs_cron',
        # env['runbot_merge.commit']._notify()
        'runbot_merge.process_updated_commits',
        # env['runbot_merge.project']._check_stagings()
        'runbot_merge.merge_cron',
        # env['runbot_merge.project']._create_stagings()
        'runbot_merge.staging_cron',
        # env['runbot_merge.pull_requests']._check_linked_prs_statuses()
        'runbot_merge.check_linked_prs_status',
        # env['runbot_merge.pull_requests.feedback']._send()
        'runbot_merge.feedback_cron',
    ]

@pytest.fixture
def project(env, config):
    return env['runbot_merge.project'].create({
        'name': 'odoo',
        'github_token': config['github']['token'],
        'github_prefix': 'hansen',
        'branch_ids': [(0, 0, {'name': 'master'})],
    })
