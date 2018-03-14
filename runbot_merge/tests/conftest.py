import odoo

import pytest

import fake_github

@pytest.fixture
def gh():
    with fake_github.Github() as gh:
        yield gh

def pytest_addoption(parser):
    parser.addoption("--db", action="store", help="Odoo DB to run tests with")
    parser.addoption('--addons-path', action='store', help="Odoo's addons path")

@pytest.fixture(scope='session')
def registry(request):
    """ Set up Odoo & yields a registry to the specified db
    """
    db = request.config.getoption('--db')
    addons = request.config.getoption('--addons-path')
    odoo.tools.config.parse_config(['--addons-path', addons, '-d', db, '--db-filter', db])
    try:
        odoo.service.db._create_empty_database(db)
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

        yield odoo.registry(db)

@pytest.fixture
def env(request, registry):
    """Generates an environment, can be parameterized on a user's login
    """
    with registry.cursor() as cr:
        env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
        login = getattr(request, 'param', 'admin')
        if login != 'admin':
            user = env['res.users'].search([('login', '=', login)], limit=1)
            env = odoo.api.Environment(cr, user.id, {})
        ctx = env['res.users'].context_get()
        registry.enter_test_mode(cr)
        yield env(context=ctx)
        registry.leave_test_mode()

        cr.rollback()

