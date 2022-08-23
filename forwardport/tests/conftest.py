# -*- coding: utf-8 -*-
import re

import pytest
import requests

@pytest.fixture
def default_crons():
    return [
        'runbot_merge.process_updated_commits',
        'runbot_merge.merge_cron',
        'runbot_merge.staging_cron',
        'forwardport.port_forward',
        'forwardport.updates',
        'runbot_merge.check_linked_prs_status',
        'runbot_merge.feedback_cron',
    ]

# public_repo — necessary to leave comments
# admin:repo_hook — to set up hooks (duh)
# delete_repo — to cleanup repos created under a user
# user:email — fetch token/user's email addresses
TOKEN_SCOPES = {
    'github': {'admin:repo_hook', 'delete_repo', 'public_repo', 'user:email'},
    # TODO: user:email so they can fetch the user's email?
    'role_reviewer': {'public_repo'},# 'delete_repo'},
    'role_self_reviewer': {'public_repo'},# 'delete_repo'},
    'role_other': {'public_repo'},# 'delete_repo'},
}
@pytest.fixture(autouse=True, scope='session')
def _check_scopes(config):
    for section, vals in config.items():
        required_scopes = TOKEN_SCOPES.get(section)
        if required_scopes is None:
            continue

        response = requests.get('https://api.github.com/rate_limit', headers={
            'Authorization': 'token %s' % vals['token']
        })
        assert response.status_code == 200
        x_oauth_scopes = response.headers['X-OAuth-Scopes']
        token_scopes = set(re.split(r',\s+', x_oauth_scopes))
        assert token_scopes >= required_scopes, \
            "%s should have scopes %s, found %s" % (section, token_scopes, required_scopes)

@pytest.fixture()
def module():
    """ When a test function is (going to be) run, selects the containing
    module (as needing to be installed)
    """
    # NOTE: no request.fspath (because no request.function) in session-scoped fixture so can't put module() at the toplevel
    return 'forwardport'
