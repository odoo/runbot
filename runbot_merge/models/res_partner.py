import collections
import random
from email.utils import parseaddr

from markupsafe import Markup

from odoo import fields, models, tools, api, Command

from .. import github

LIST_TEMPLATE = Markup("<ul>{}</ul>")
OVERRIDE_ADD_TEMPLATE = Markup("<li>added override rights for {!r} on {}</li>")
OVERRIDE_REMOVE_TEMPLATE = Markup("<li>removed override rights for {!r} on {}</li>")

class CIText(fields.Char):
    type = 'char'
    column_type = ('citext', 'citext')
    column_cast_from = ('varchar', 'text')

class Partner(models.Model):
    _name = 'res.partner'
    _inherit = ['res.partner', 'mail.thread']

    email = fields.Char(index=True)
    github_login = CIText(tracking=True)
    delegate_reviewer = fields.Many2many('runbot_merge.pull_requests')
    formatted_email = fields.Char(string="commit email", compute='_rfc5322_formatted')
    review_rights = fields.One2many('res.partner.review', 'partner_id')
    override_rights = fields.Many2many('res.partner.override')
    override_sensitive = fields.Boolean(compute="_compute_sensitive_overrides")

    def _auto_init(self):
        res = super(Partner, self)._auto_init()
        tools.create_unique_index(
            self._cr, 'runbot_merge_unique_gh_login', self._table, ['github_login'])
        return res

    @api.depends('name', 'email', 'github_login')
    def _rfc5322_formatted(self):
        for partner in self:
            if partner.email:
                email = parseaddr(partner.email)[1]
            elif partner.github_login:
                email = '%s@users.noreply.github.com' % partner.github_login
            else:
                email = ''
            partner.formatted_email = '%s <%s>' % (partner.name, email)

    @api.depends("override_rights.context")
    def _compute_sensitive_overrides(self):
        for p in self:
            p.override_sensitive = any(o.context == 'ci/security' for o in p.override_rights)

    def write(self, vals):
        created = []
        updated = {}
        deleted = set()
        for cmd, id, values in vals.get('review_rights', []):
            if cmd == Command.DELETE:
                deleted.add(id)
            elif cmd == Command.CREATE:
                # 'repository_id': 3, 'review': True, 'self_review': False
                created.append(values)
            elif cmd == Command.UPDATE:
                updated[id] = values
            #  could also be LINK for records which are not touched but we don't care

        if 'override_rights' in vals:
            overrides_before = {p: p.override_rights for p in self}
        else:
            overrides_before = {}

        Repo = self.env['runbot_merge.repository'].browse
        for p in self:
            msgs = []
            if ds := p.review_rights.filtered(lambda r: r.id in deleted):
                msgs.append("removed review rights on {}\n".format(
                    ', '.join(ds.mapped('repository_id.name'))
                ))
            if us := p.review_rights.filtered(lambda r: r.id in updated):
                msgs.extend(
                    "updated review rights on {}: {}\n".format(
                        u.repository_id.name,
                        ', '.join(
                            f'allowed {f}' if v else f'forbid {f}'
                            for f in ['review', 'self_review']
                            if (v := updated[u.id].get(f)) is not None
                        )
                    )
                    for u in us
                )
            msgs.extend(
                'added review rights on {}: {}\n'.format(
                    Repo(c['repository_id']).name,
                    ', '.join(filter(c.get, ['review', 'self_review'])),
                )
                for c in created
            )
            if msgs:
                p._message_log(body=LIST_TEMPLATE.format(Markup().join(
                    map(Markup('<li>{}</li>').format, reversed(msgs))
                )))

        r = super().write(vals)
        if 'override_rights' in vals:
            for p in self:
                overrides_after = p.override_rights
                msgs = []
                for o in overrides_after - overrides_before[p]:
                    msgs.append(OVERRIDE_ADD_TEMPLATE.format(o.context, o.repository_id.name))
                for o in overrides_before[p] - overrides_after:
                    msgs.append(OVERRIDE_REMOVE_TEMPLATE.format(o.context, o.repository_id.name))
                match msgs:
                    case []:
                        continue
                    case [msg]:
                        p._message_log(body=msg)
                    case msgs:
                        p._message_log(body=LIST_TEMPLATE.format(Markup().join(msgs)))

        return r


class PartnerMerge(models.TransientModel):
    _inherit = 'base.partner.merge.automatic.wizard'

    @api.model
    def _update_values(self, src_partners, dst_partner):
        # sift down through src partners, removing all github_login and keeping
        # the last one
        new_login = None
        for p in src_partners:
            new_login = p.github_login or new_login
        if new_login:
            src_partners.write({'github_login': False})
        if new_login and not dst_partner.github_login:
            dst_partner.github_login = new_login
        super()._update_values(src_partners, dst_partner)

class ReviewRights(models.Model):
    _name = 'res.partner.review'
    _description = "mapping of review rights between partners and repos"

    partner_id = fields.Many2one('res.partner', required=True, ondelete='cascade')
    repository_id = fields.Many2one('runbot_merge.repository', required=True)
    review = fields.Boolean(default=False)
    self_review = fields.Boolean(default=False)

    def _auto_init(self):
        res = super()._auto_init()
        tools.create_unique_index(self._cr, 'runbot_merge_review_m2m', self._table, ['partner_id', 'repository_id'])
        return res

    @api.depends('repository_id.name', 'review', 'self_review')
    def _compute_display_name(self):
        for r in self:
            r.display_name = '%s: %s' % (r.repository_id.name, ', '.join(filter(None, [
                r.review and "reviewer",
                r.self_review and "self-reviewer"
            ])))

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        return self.search((args or []) + [('repository_id.name', operator, name)], limit=limit).name_get()

class OverrideRights(models.Model):
    _name = 'res.partner.override'
    _description = 'lints which the partner can override'

    partner_ids = fields.Many2many('res.partner')
    repository_id = fields.Many2one('runbot_merge.repository')
    context = fields.Char(required=True)

    def init(self):
        super().init()
        tools.create_unique_index(
            self.env.cr, 'res_partner_override_unique', self._table,
            ['context', 'coalesce(repository_id, 0)']
        )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            for partner in rec.partner_ids:
                partner._message_log(
                    body=f"added override rights for {rec.context!r} on {rec.repository_id.name}"
                )
        return records

    def write(self, vals):
        if 'partner_ids' in vals:
            partners_before = {r: r.partner_ids for r in self}
        else:
            partners_before = {}

        r = super().write(vals)
        if 'partner_ids' in vals:
            msgs = collections.defaultdict(list)
            for o in self:
                partners_after = o.partner_ids
                for p in partners_after - partners_before[o]:
                    msgs[p].append(OVERRIDE_ADD_TEMPLATE.format(o.context, o.repository_id.name))
                for p in partners_before[o] - partners_after:
                    msgs[p].append(OVERRIDE_REMOVE_TEMPLATE.format(o.context, o.repository_id.name))
            for p, msgs in msgs.items():
                match msgs:
                    case []:
                        continue
                    case [msg]:
                        p._message_log(body=msg)
                    case msgs:
                        p._message_log(body=LIST_TEMPLATE.format(Markup().join(msgs)))

        return r

    def unlink(self):
        for o in self:
            for p in o.partner_ids:
                p._message_log(body=f"removed override rights for {o.context!r} on {o.repository_id.name}")
        return super().unlink()

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        return self.search((args or []) + [
            '|', ('context', operator, name),
                 ('repository_id.name', operator, name)
        ], limit=limit).name_get()

    @api.depends('repository_id.name', 'context')
    def _compute_display_name(self):
        for r in self:
            if r.repository_id:
                r.display_name = f'{r.repository_id.name}: {r.context}'
            else:
                r.display_name = r.context
