import datetime
import time

import jwt
import requests

from odoo import models, fields
from odoo.sql_db import db_connect

#: {owner: (iat, expiry)}
iats: dict[str, tuple[str, int]] = {}  # TODO: per repo?


class Installation(models.Model):
    _name = "runbot_merge.github_connector.installation"
    _description = "App installation info"

    connector_id = fields.Many2one('runbot_merge.github_connector', required=True, index=True)
    # technically we could reuse github's iids as the record's id but that
    # requires raw SQL insertion
    installation_id = fields.Integer(required=True)
    owner_id = fields.Integer(required=True)
    owner_login = fields.Char(required=True)
    repository_selection = fields.Selection([
        ('all', 'All'),
        ('selected', 'Selected'),
    ])
    repositories = fields.Json(default=[])


class Connector(models.Model):
    _name = 'runbot_merge.github_connector'
    _description = "Configurator for github interactions (git calls, API calls, webhooks)"

    name = fields.Char(required=True)
    method = fields.Selection([
        ('pat', "Interact using Personal Access Tokens"),
        ('app', "Interact using a github app"),
    ], required=True)

    def _main_token(self, repository: str) -> str:
        match self.method:
            case 'pat':
                return self.main_pat
            case 'app':
                owner, _ = repository.split('/')
                # iats are short lived (60mn) and there can be concurrent iats,
                # so use a per-worker in-memory cache to store them
                token, expiry = iats.get(owner, (None, 0))
                if expiry >= time.time():
                    return token

                with requests.Session() as s:
                    token = jwt.encode(
                        {
                            "iat": int(time.time()) - 60,
                            "exp": int(time.time()) + 600,
                            "iss": self.client_id,
                        },
                        self.private_key.encode(),
                        algorithm="RS256",
                    )
                    s.headers.update({
                        'Accept': 'application/vnd.github+json',
                        'Authorization': f'Bearer {token}',
                        'Content-Type': 'application/json',
                    })

                    # if no iat, look for an installation
                    iid = next((
                        inst.installation_id
                        for inst in self.installations
                        # TODO: owner renaming themselves?
                        if inst.owner_login == owner
                        # TODO: check repos?
                    ), None)
                    # if no installation, check if there's one we don't know about
                    if iid is None:
                        res = s.get(f'https://api.github.com/repos/{repository}/installation')
                        if not res.ok:
                            raise ValueError(f"No installation found for {repository}")
                        installation = res.json()
                        self.env['runbot_merge.github_connector.installation'].create({
                            'connector_id': self.id,
                            'installation_id': installation['id'],
                            'owner_id': installation['account']['id'],
                            'owner_login': installation['account']['login'],
                            'repository_selection': installation['repository_selection'],
                            # FIXME: retrieve the repos? Or are they provided if repository_selection: selected
                        })
                        iid = installation['id']

                    res = s.post(f'https://api.github.com/app/installations/{iid}/access_tokens', json={})
                    if not res.ok:
                        raise ValueError(f"Unable to create access token for {repository}: {res.text}")
                    iat = res.json()
                    iats[owner] = (
                        iat["token"],
                        # iat ttl is 1h, remove 10 minutes to limit chances of
                        # hitting an expired token in the middle of something
                        int(datetime.datetime.fromisoformat(iat["expires_at"]).timestamp()) - 600,
                    )
                return iat['token']

        raise ValueError(f"Unknown connection method {self.method!r}")

    def _fp_token(self, repository) -> str:
        match self.method:
            case 'pat':
                return self.fp_pat
            case 'app':
                if self.refresh_token and self.auth_expires_at < fields.Datetime.now():
                    # after we've called `refresh_token` we can't risk a
                    # rollback, so update the record in a separate transaction...?
                    cn = db_connect(self.env.cr.dbname)
                    with cn.cursor() as cr:
                        cr.execute('SELECT FROM runbot_merge_github_connector WHERE id = %s FOR NO KEY UPDATE', [self.id])
                        slef = self.env(cr=cr)[self._name].browse(self.id)
                        r = requests.post(
                            "https://github.com/login/oauth/access_token",
                            params={
                                'client_id': slef.client_id,
                                'client_secret': slef.client_secret,
                                'grant_type': 'refresh_token',
                                'refresh_token': slef.refresh_token,
                            }
                        )
                        r.raise_for_status()
                        response = r.json()
                        # same as IAT, adjust by about 10mn to limit risk of a
                        # token expiring in the middle of an operation
                        expiry = fields.Datetime.now() + datetime.timedelta(
                            response['expires_in'] - 600,
                        )
                        slef.write({
                            'access_token': response['access_token'],
                            'refresh_token': response['refresh_token'],
                            'auth_expires_at': expiry,
                        })
                        return response['access_token']

                # the UATs are much longer lived, and more importantly there can
                # only be one at a time (refreshing an UAT invalidates the
                # previous one), so they're stored in database, and refreshing
                # an UAT has to acquire a full lock on the record
                return self.auth_token
        raise ValueError(f"Unknown connection method {self.method!r}")

    # region pat
    main_pat = fields.Char(
        "Main Token",
        groups="base.group_system",
        help="Token for interactions with the main repository (comments, statuses, etc...)",
    )
    fp_pat = fields.Char(
        "Forward-Port Token",
        groups="base.group_system",
        help="Token for interactions with the fp repository (updating fp code,"
             " creating fp PRs to the main repository)",
    )
    # endregion

    # region app
    client_id = fields.Char()
    client_secret = fields.Char(groups="base.group_system")
    private_key = fields.Text(groups="base.group_system")
    webhook_secret = fields.Char(groups="base.group_system")
    # can really only have one at a time, so needs to be durable / shared
    auth_token = fields.Char(groups="base.group_system")
    refresh_token = fields.Char(groups="base.group_system")
    auth_expires_at = fields.Datetime()

    installations = fields.One2many('runbot_merge.github_connector.installation', 'connector_id')
    # endregion