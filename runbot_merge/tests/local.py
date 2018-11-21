# -*- coding: utf-8 -*-
import inspect

import pytest
import werkzeug.test, werkzeug.wrappers

import odoo

import fake_github

@pytest.fixture(scope='session')
def remote_p():
    return False

@pytest.fixture
def gh():
    with fake_github.Github() as gh:
        yield gh

@pytest.fixture(scope='session')
def registry(request):
    """ Set up Odoo & yields a registry to the specified db
    """
    db = request.config.getoption('--db')
    addons = request.config.getoption('--addons-path')
    odoo.tools.config.parse_config(['--addons-path', addons, '-d', db, '--db-filter', db])
    try:
        odoo.service.db._create_empty_database(db)
        odoo.service.db._initialize_db(None, db, False, False, 'admin')
    except odoo.service.db.DatabaseExists:
        pass

    #odoo.service.server.load_server_wide_modules()
    #odoo.service.server.preload_registries([db])

    with odoo.api.Environment.manage():
        # ensure module is installed
        r0 = odoo.registry(db)
        with r0.cursor() as cr:
            env = odoo.api.Environment(cr, 1, {})
            [mod] = env['ir.module.module'].search([('name', '=', 'runbot_merge')])
            mod.button_immediate_install()

        from odoo.addons.runbot_merge.models import pull_requests
        pull_requests.STAGING_SLEEP = 0
        yield odoo.registry(db)

@pytest.fixture
def cr(registry):
    # in v12, enter_test_mode flags an existing cursor while in v11 it sets one up
    if inspect.signature(registry.enter_test_mode).parameters:
        with registry.cursor() as cr:
            registry.enter_test_mode(cr)
            yield cr
            registry.leave_test_mode()
            cr.rollback()
    else:
        registry.enter_test_mode()
        with registry.cursor() as cr:
            yield cr
            cr.rollback()
        registry.leave_test_mode()

@pytest.fixture
def env(cr):
    env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
    ctx = env['res.users'].context_get()
    yield env(context=ctx)

@pytest.fixture
def owner():
    return 'user'

@pytest.fixture(autouse=True)
def users(env):
    env['res.partner'].create({
        'name': "Reviewer",
        'github_login': 'reviewer',
        'reviewer': True,
        'email': "reviewer@example.com",
    })
    env['res.partner'].create({
        'name': "Self Reviewer",
        'github_login': 'self_reviewer',
        'self_reviewer': True,
    })

    return {
        'reviewer': 'reviewer',
        'self_reviewer': 'self_reviewer',
        'other': 'other',
        'user': 'user',
    }

@pytest.fixture
def project(env):
    return env['runbot_merge.project'].create({
        'name': 'odoo',
        'github_token': 'okokok',
        'github_prefix': 'hansen',
        'branch_ids': [(0, 0, {'name': 'master'})],
        'required_statuses': 'legal/cla,ci/runbot',
    })

@pytest.fixture
def make_repo(gh, project):
    def make_repo(name):
        fullname = 'org/' + name
        project.write({'repo_ids': [(0, 0, {'name': fullname})]})
        return gh.repo(fullname, hooks=[
            ((odoo.http.root, '/runbot_merge/hooks'), [
                'pull_request', 'issue_comment', 'status', 'pull_request_review'
            ])
        ])
    return make_repo

@pytest.fixture
def page():
    c = werkzeug.test.Client(odoo.http.root, werkzeug.wrappers.BaseResponse)
    def get(url):
        r = c.get(url)
        assert r.status_code == 200
        return r.data
    return get

# TODO: project fixture
# TODO: repos (indirect/parameterize?) w/ WS hook
# + repo proxy object
