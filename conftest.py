# -*- coding: utf-8 -*-
import configparser
import re
import subprocess
import time
import uuid

import psutil
import pytest
import requests

NGROK_CLI = [
    'ngrok', 'start', '--none', '--region', 'eu',
]

def pytest_addoption(parser):
    parser.addoption('--addons-path')
    parser.addoption('--db', help="DB to run the tests against", default=str(uuid.uuid4()))
    parser.addoption("--no-delete", action="store_true", help="Don't delete repo after a failed run")

    parser.addoption(
        '--tunnel', action="store", type="choice", choices=['ngrok', 'localtunnel'], default='ngrok',
        help="Which tunneling method to use to expose the local Odoo server "
             "to hook up github's webhook. ngrok is more reliable, but "
             "creating a free account is necessary to avoid rate-limiting "
             "issues (anonymous limiting is rate-limited at 20 incoming "
             "queries per minute, free is 40, multi-repo batching tests will "
             "blow through the former); localtunnel has no rate-limiting but "
             "the servers are way less reliable")

def pytest_report_header(config):
    return 'Running against database ' + config.getoption('--db')

@pytest.fixture(scope="session")
def config(pytestconfig):
    """ Flat version of the pytest config file (pytest.ini), parses to a
    simple dict of {section: {key: value}}

    """
    conf = configparser.ConfigParser(interpolation=None)
    conf.read([pytestconfig.inifile])
    return {
        name: dict(s.items())
        for name, s in conf.items()
    }

@pytest.fixture(scope='session')
def rolemap(config):
    # only fetch github logins once per session
    rolemap = {}
    for k, data in config.items():
        if k.startswith('role_'):
            role = k[5:]
        elif k == 'github':
            role = 'user'
        else:
            continue

        r = requests.get('https://api.github.com/user', headers={'Authorization': 'token %s' % data['token']})
        r.raise_for_status()

        rolemap[role] = data['user'] = r.json()['login']
    return rolemap

# apparently conftests can override one another's fixtures but plugins can't
# override conftest fixtures (?) so if this is defined as "users" it replaces
# the one from runbot_merge/tests/local and everything breaks.
#
# Alternatively this could be special-cased using remote_p or something but
# that's even more gross. It might be possible to handle that via pytest's
# hooks as well but I didn't check
@pytest.fixture
def users_(env, config, rolemap):
    for role, login in rolemap.items():
        if role in ('user', 'other'):
            continue

        env['res.partner'].create({
            'name': config['role_' + role].get('name', login),
            'github_login': login,
            'reviewer': role == 'reviewer',
            'self_reviewer': role == 'self_reviewer',
        })

    return rolemap

@pytest.fixture(scope='session')
def tunnel(pytestconfig, port):
    """ Creates a tunnel to localhost:<port> using ngrok or localtunnel, should yield the
    publicly routable address & terminate the process at the end of the session
    """

    tunnel = pytestconfig.getoption('--tunnel')
    if tunnel == 'ngrok':
        addr = 'localhost:%d' % port
        # if ngrok is not running, start it
        try:
            # FIXME: use config file so we can set web_addr to something else
            #        than localhost:4040 (otherwise we can't disambiguate
            #        between the ngrok we started and an ngrok started by
            #        some other user)
            requests.get('http://localhost:4040/api')
        except requests.exceptions.ConnectionError:
            subprocess.Popen(NGROK_CLI, stdout=subprocess.DEVNULL)
            time.sleep(1)

        requests.post('http://localhost:4040/api/tunnels', json={
            'name': str(port),
            'proto': 'http',
            'bind_tls': True,
            'addr': addr,
            'inspect': False,
        })
        time.sleep(5)

        try:
            r = requests.get('http://localhost:4040/api/tunnels')
            r.raise_for_status()
            yield next(
                t['public_url']
                for t in r.json()['tunnels']
                if t['proto'] == 'https'
                if t['config']['addr'].endswith(addr)
            )
        finally:
            requests.delete('http://localhost:4040/api/tunnels/%s' % port)
            time.sleep(5) # apparently tearing down the tunnel can take some time
            r = requests.get('http://localhost:4040/api/tunnels')
            if r.ok and r.json()['tunnels']:
                return

            # ngrok is broken or all tunnels have been shut down -> try to
            # find and kill it (but only if it looks a lot like we started it)
            for p in psutil.process_iter():
                if p.name() == 'ngrok' and p.cmdline() == NGROK_CLI:
                    p.terminate()
                    break

    elif tunnel == 'localtunnel':
        p = subprocess.Popen(['lt', '-p', str(port)], stdout=subprocess.PIPE)
        try:
            r = p.stdout.readline()
            m = re.match(br'your url is: (https://.*\.localtunnel\.me)', r)
            assert m, "could not get the localtunnel URL"
            yield m.group(1).decode('ascii')
        finally:
            p.terminate()
            p.wait(30)
    else:
        raise ValueError("Unsupported %s tunnel method" % tunnel)

@pytest.fixture(scope='session')
def dbcache(request, module):
    """ Creates template DB once per run, then just duplicates it before
    starting odoo and running the testcase
    """
    db = request.config.getoption('--db')
    subprocess.run([
        'odoo', '--no-http',
        '--addons-path', request.config.getoption('--addons-path'),
        '-d', db, '-i', module,
        '--max-cron-threads', '0',
        '--stop-after-init'
    ], check=True)
    yield db
    subprocess.run(['dropdb', db])

@pytest.fixture
def db(request, dbcache):
    rundb = str(uuid.uuid4())
    subprocess.run(['createdb', '-T', dbcache, rundb], check=True)

    yield rundb

    if not request.config.getoption('--no-delete'):
        subprocess.run(['dropdb', rundb], check=True)
