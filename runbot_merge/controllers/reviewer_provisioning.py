# -*- coding: utf-8 -*-
import logging

from odoo import Command
from odoo.http import Controller, request, route

try:
    from odoo.addons.saas_worker.util import from_role
except ImportError:
    def from_role(*_, **__):
        return lambda _: None

_logger = logging.getLogger(__name__)
class MergebotReviewerProvisioning(Controller):
    @from_role('accounts', signed=True)
    @route('/runbot_merge/users', type='json', auth='public')
    def list_users(self):
        env = request.env(su=True)
        return [{
                'github_login': u.github_login,
                'email': u.email,
            }
            for u in env['res.users'].search([])
            if u.github_login
        ]

    @from_role('accounts', signed=True)
    @route('/runbot_merge/provision', type='json', auth='public')
    def provision_user(self, users):
        _logger.info('Provisioning %s users: %s.', len(users), ', '.join(map(
            '{email} ({github_login})'.format_map,
            users
        )))
        env = request.env(su=True)
        Partners = env['res.partner']
        Users = env['res.users']

        existing_logins = set()
        existing_oauth = set()
        for u in Users.with_context(active_test=False).search([]):
            existing_logins.add(u.login)
            existing_oauth .add((u.oauth_provider_id.id, u.oauth_uid))
        existing_partners = Partners.with_context(active_test=False).search([
            '|', ('email', 'in', [u['email'] for u in users]),
                 ('github_login', 'in', [u['github_login'] for u in users])
        ])
        _logger.info("Found %d existing matching partners.", len(existing_partners))
        partners = {}
        for p in existing_partners:
            if p.email:
                # email is not unique, though we want it to be (probably)
                current = partners.get(p.email)
                if current:
                    _logger.warning(
                        "Lookup conflict: %r set on two partners %r and %r.",
                        p.email, current.display_name, p.display_name,
                    )
                else:
                    partners[p.email] = p

            if p.github_login:
                # assume there can't be an existing one because github_login is
                # unique, and should not be able to collide with emails
                partners[p.github_login.casefold()] = p

        portal = env.ref('base.group_portal')
        internal = env.ref('base.group_user')
        odoo_provider = env.ref('auth_oauth.provider_openerp')

        to_create = []
        updated = 0
        to_activate = Partners
        for new in users:
            if 'sub' in new:
                new['oauth_provider_id'] = odoo_provider.id
                new['oauth_uid'] = new.pop('sub')

            # prioritise by github_login as that's the unique-est point of information
            current = partners.get(new['github_login'].casefold()) or partners.get(new['email']) or Partners
            if not current.active:
                to_activate |= current
            # entry doesn't have user -> create user
            if not current.user_ids:
                if not new['email']:
                    _logger.info(
                        "Unable to create user for %s: no email in provisioning data",
                        current.display_name
                    )
                    continue
                if 'oauth_uid' in new:
                    if (new['oauth_provider_id'], new['oauth_uid']) in existing_oauth:
                        _logger.warning(
                            "Attempted to create user with duplicate oauth uid "
                            "%s with provider %r for provisioning entry %r. "
                            "There is likely a duplicate partner (one version "
                            "with email, one with github login)",
                            new['oauth_uid'], odoo_provider.display_name, new,
                        )
                        continue
                if new['email'] in existing_logins:
                    _logger.warning(
                        "Attempted to create user with duplicate login %s for "
                        "provisioning entry %r. There is likely a duplicate "
                        "partner (one version with email, one with github "
                        "login)",
                        new['email'], new,
                    )
                    continue

                new['login'] = new['email']
                new['groups_id'] = [Command.link(internal.id)]
                # entry has partner -> create user linked to existing partner
                # (and update partner implicitly)
                if current:
                    new['partner_id'] = current.id
                to_create.append(new)
                continue

            # otherwise update user (if there is anything to update)
            user = current.user_ids
            if len(user) != 1:
                _logger.warning("Got %d users for partner %s, updating first.", len(user), current.display_name)
                user = user[:1]
            new.setdefault("active", True)
            update_vals = {
                k: v
                for k, v in new.items()
                if v != (user[k] if k != 'oauth_provider_id' else user[k].id)
            }
            if user.has_group('base.group_portal'):
                update_vals['groups_id'] = [
                    Command.unlink(portal.id),
                    Command.link(internal.id),
                ]

            if update_vals:
                user.write(update_vals)
                updated += 1

        created = len(to_create)
        if to_create:
            # only create 100 users at a time to avoid request timeout
            Users.create(to_create)

        if to_activate:
            to_activate.active = True

        _logger.info("Provisioning: created %d updated %d.", created, updated)
        return [created, updated]

    @from_role('accounts', signed=True)
    @route(['/runbot_merge/get_reviewers'], type='json', auth='public')
    def fetch_reviewers(self, **kwargs):
        reviewers = request.env['res.partner.review'].sudo().search([
            '|', ('review', '=', True), ('self_review', '=', True)
        ]).mapped('partner_id.github_login')
        return reviewers

    @from_role('accounts', signed=True)
    @route(['/runbot_merge/remove_reviewers'], type='json', auth='public', methods=['POST'])
    def update_reviewers(self, github_logins, **kwargs):
        partners = request.env['res.partner'].sudo().search([('github_login', 'in', github_logins)])
        partners.write({
            'email': False,
            'review_rights': [Command.clear()],
            'delegate_reviewer': [Command.clear()],
        })

        # Assign the linked users as portal users
        partners.mapped('user_ids').write({
            'groups_id': [Command.set([request.env.ref('base.group_portal').id])]
        })
        return True
