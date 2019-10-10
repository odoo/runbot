# -*- coding: utf-8 -*-
import pathlib
import re
import requests
from shutil import rmtree

import pytest

from odoo.tools.appdirs import user_cache_dir

@pytest.fixture
def default_crons():
    return [
        'runbot_merge.process_updated_commits',
        'runbot_merge.merge_cron',
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

@pytest.fixture(autouse=True)
def _cleanup_cache(config, users):
    """ forwardport has a repo cache which it assumes is unique per name
    but tests always use the same repo paths / names for different repos
    (the repos get re-created), leading to divergent repo histories.

    So clear cache after each test, two tests should not share repos.
    """
    yield
    cache_root = pathlib.Path(user_cache_dir('forwardport'))
    rmtree(cache_root / config['github']['owner'], ignore_errors=True)
    for login in users.values():
        rmtree(cache_root / login, ignore_errors=True)

@pytest.fixture(scope='session')
def module():
    """ When a test function is (going to be) run, selects the containing
    module (as needing to be installed)
    """
    # NOTE: no request.fspath (because no request.function) in session-scoped fixture so can't put module() at the toplevel
    return 'forwardport'
