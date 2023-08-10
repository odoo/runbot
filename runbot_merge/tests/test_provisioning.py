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
    internal = env.ref('base.group_user')
    assert (g.groups_id & internal) == internal, "check that users were provisioned as internal (not portal)"

    # repeated provisioning should be a no-op
    r = provision_user(port, [GEORGE])
    assert r == [0, 0]

    # the email (real login) should be the determinant, any other field is
    # updatable
    r = provision_user(port, [{**GEORGE, 'name': "x"}])
    assert r == [0, 1]

    r = provision_user(port, [dict(GEORGE, name="x", github_login="y", sub="42")])
    assert r == [0, 1]

    r = provision_user(port, [dict(GEORGE, active=False)])
    assert r == [0, 1]
    assert not env['res.users'].search([('login', '=', GEORGE['email'])])
    assert env['res.partner'].search([('email', '=', GEORGE['email'])])

def test_upgrade_partner(env, port):
    # matching partner with an email but no github login
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

    # matching partner with a github login but no email
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

    # matching partner with a deactivated user
    p.user_ids.active = False
    r = provision_user(port, [GEORGE])
    assert r == [0, 1]
    assert len(p.user_ids) == 1, "provisioning should re-enable user"
    assert p.user_ids.active

    # matching deactivated partner (with a deactivated user)
    p.user_ids.active = False
    p.active = False
    r = provision_user(port, [GEORGE])
    assert r == [0, 1]
    assert p.active, "provisioning should re-enable partner"
    assert p.user_ids.active

def test_duplicates(env, port):
    """In case of duplicate data, the handler should probably not blow up, but
    instead log a warning (so the data gets fixed eventually) and skip
    """
    # dupe 1: old oauth signup account & github interaction account, provisioning
    # prioritises the github account & tries to create a user for it, which
    # fails because the signup account has the same oauth uid (probably)
    env['res.partner'].create({'name': 'foo', 'github_login': 'foo'})
    env['res.users'].create({'login': 'foo@example.com', 'name': 'foo', 'email': 'foo@example.com', 'oauth_provider_id': 1, 'oauth_uid': '42'})
    assert provision_user(port, [{
        'name': "foo",
        'email': 'foo@example.com',
        'github_login': 'foo',
        'sub': '42'
    }]) == [0, 0]

    # dupe 2: old non-oauth signup account & github interaction account, same
    # as previous except it breaks on the login instead of the oauth_uid
    env['res.partner'].create({'name': 'bar', 'github_login': 'bar'})
    env['res.users'].create({'login': 'bar@example.com', 'name': 'bar', 'email': 'bar@example.com'})
    assert provision_user(port, [{
        'name': "bar",
        'email': 'bar@example.com',
        'github_login': 'bar',
        'sub': '43'
    }]) == [0, 0]

def test_no_email(env, port):
    """ Provisioning system should ignore email-less entries
    """
    r = provision_user(port, [{**GEORGE, 'email': None}])
    assert r == [0, 0]

def test_casing(env, port):
    p = env['res.partner'].create({
        'name': 'Bob',
        'github_login': "Bob",
    })
    assert not p.user_ids
    assert provision_user(port, [{
        'name': "Bob Thebuilder",
        'github_login': "bob",
        'email': 'bob@example.org',
        'sub': '5473634',
    }]) == [1, 0]

    assert p.user_ids.name == 'Bob Thebuilder'
    assert p.user_ids.email == 'bob@example.org'
    assert p.user_ids.oauth_uid == '5473634'
    # should be written on the partner through the user
    assert p.name == 'Bob Thebuilder'
    assert p.email == 'bob@example.org'
    assert p.github_login == 'bob'

def test_user_leaves_and_returns(env, port):
    internal = env.ref('base.group_user')
    portal = env.ref('base.group_portal')
    categories = internal | portal | env.ref('base.group_public')

    assert provision_user(port, [{
        "name": "Bamien Douvy",
        "github_login": "DouvyB",
        "email": "bado@example.org",
        "sub": "123456",
    }]) == [1, 0]
    p = env['res.partner'].search([('github_login', '=', "DouvyB")])
    assert (p.user_ids.groups_id & categories) == internal

    # bye bye 👋
    requests.post(f'http://localhost:{port}/runbot_merge/remove_reviewers', json={
        'jsonrpc': '2.0',
        'id': None,
        'method': 'call',
        'params': {'github_logins': ['douvyb']},
    })
    assert (p.user_ids.groups_id & categories) == portal
    assert p.email is False

    # he's back ❤️
    assert provision_user(port, [{
        "name": "Bamien Douvy",
        "github_login": "DouvyB",
        "email": "bado@example.org",
        "sub": "123456",
    }]) == [0, 1]
    assert (p.user_ids.groups_id & categories) == internal
    assert p.email == 'bado@example.org'

def test_bulk_ops(env, port):
    a, b = env['res.partner'].create([{
        'name': "Bob",
        'email': "bob@example.org",
        'active': False,
    }, {
        'name': "Coc",
        'email': "coc@example.org",
        'active': False,
    }])
    assert a.active is b.active is False

    assert provision_user(port, [
        {'email': 'bob@example.org', 'github_login': 'xyz'},
        {'email': 'coc@example.org', 'github_login': 'abc'},
    ]) == [2, 0]
    assert a.users_id
    assert b.users_id
    assert a.active is b.active is True

def provision_user(port, users):
    r = requests.post(f'http://localhost:{port}/runbot_merge/provision', json={
        'jsonrpc': '2.0',
        'id': None,
        'method': 'call',
        'params': {'users': users},
    })
    r.raise_for_status()
    json = r.json()
    assert 'error' not in json, json['error']['data']['debug']

    return json['result']
