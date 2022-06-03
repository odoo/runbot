import pytest
import requests

GEORGE = {
    'name': "George Pearce",
    'email': 'george@example.org',
    'github_login': 'emubitch',
    'sub': '19321102'
}
def test_basic_provisioning(env, port):

    r = provision_user(port, [GEORGE])
    assert r == [1, 0]

    g = env['res.users'].search([('login', '=', GEORGE['email'])])
    assert g.partner_id.name == GEORGE['name']
    assert g.partner_id.github_login == GEORGE['github_login']
    assert g.oauth_uid == GEORGE['sub']

    # repeated provisioning should be a no-op
    r = provision_user(port, [GEORGE])
    assert r == [0, 0]

    # the email (real login) should be the determinant, any other field is
    # updatable
    r = provision_user(port, [{**GEORGE, 'name': "x"}])
    assert r == [0, 1]

    r = provision_user(port, [dict(GEORGE, name="x", github_login="y", sub="42")])
    assert r == [0, 1]

    # can't fail anymore because github_login now used to look up the existing
    # user
    # with pytest.raises(Exception):
    #     provision_user(port, [{
    #         'name': "other@example.org",
    #         'email': "x",
    #         'github_login': "y",
    #         'sub': "42"
    #     }])

    r = provision_user(port, [dict(GEORGE, active=False)])
    assert r == [0, 1]
    assert not env['res.users'].search([('login', '=', GEORGE['email'])])
    assert env['res.partner'].search([('email', '=', GEORGE['email'])])

def test_upgrade_partner(env, port):
    # If a partner exists for a github login (and / or email?) it can be
    # upgraded by creating a user for it
    p = env['res.partner'].create({
        'name': GEORGE['name'],
        'email': GEORGE['email'],
    })
    r = provision_user(port, [GEORGE])
    assert r == [1, 0]
    assert p.user_ids.read(['email', 'github_login', 'oauth_uid']) == [{
        'id': p.user_ids.id,
        'github_login': GEORGE['github_login'],
        'oauth_uid': GEORGE['sub'],
        'email': GEORGE['email'],
    }]

    p.user_ids.unlink()
    p.unlink()

    p = env['res.partner'].create({
        'name': GEORGE['name'],
        'github_login': GEORGE['github_login'],
    })
    r = provision_user(port, [GEORGE])
    assert r == [1, 0]
    assert p.user_ids.read(['email', 'github_login', 'oauth_uid']) == [{
        'id': p.user_ids.id,
        'github_login': GEORGE['github_login'],
        'oauth_uid': GEORGE['sub'],
        'email': GEORGE['email'],
    }]

    p.user_ids.unlink()
    p.unlink()

def provision_user(port, users):
    r = requests.post(f'http://localhost:{port}/runbot_merge/provision', json={
        'jsonrpc': '2.0',
        'id': None,
        'method': 'call',
        'params': {'users': users},
    })
    r.raise_for_status()
    json = r.json()
    assert 'error' not in json

    return json['result']
