import uuid
import pytest

pytest_plugins = ["local"]

@pytest.fixture(scope='session')
def module():
    return 'runbot_merge'
