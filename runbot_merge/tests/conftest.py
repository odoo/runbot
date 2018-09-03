pytest_plugins = ["local"]

def pytest_addoption(parser):
    parser.addoption("--db", action="store", help="Odoo DB to run tests with")
    parser.addoption('--addons-path', action='store', help="Odoo's addons path")
