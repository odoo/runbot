from datetime import datetime, timezone

import requests


def test_token_refresh(config) -> None:
    """If I simulate an invalidation of a user's (testsuite-side) token,
    check that it is automatically refreshed.
    """
    session = requests.Session()
    for k, entry in config.items():
        if type(entry) is dict:
            continue
        if entry['type'] != 'User':
            continue

        token = entry["token"]
        r = session.get(
            f'https://api.github.com/users/{entry["login"]}',
            headers={'Authorization': f'token {token}'}
        )
        assert r.status_code == 200
        id = r.json()['id']

        auth = entry['authorization']
        auth.expires_at = datetime.now(timezone.utc).isoformat()
        refresh = auth.refresh_token
        assert entry['token'] != token, \
            "should have refreshed the authorization and generated a new token"
        assert auth.refresh_token != refresh, \
            "should have refreshed the authorization and generated a new token"
        r = session.get(
            f'https://api.github.com/users/{entry["login"]}',
            headers={'Authorization': f'token {token}'}
        )
        assert r.status_code == 401, "the old token should be invalid"
        r = session.get(
            f'https://api.github.com/users/{entry["login"]}',
            headers={'Authorization': f'token {entry["token"]}'}
        )
        assert r.status_code == 200
        assert r.json()['id'] == id