import logging
import sys
from datetime import datetime, timedelta

import lxml.etree
from babel.dates import format_timedelta
from lxml.builder import E
from lxml.etree import _Element

from odoo import models, api, fields
from odoo.modules.registry import Registry


class Queue(models.BaseModel):
    _name = 'runbot_merge.queue'
    _description = "Common cron behaviour for queue-type models attached to crons"
    _inherit = ['mail.thread']
    _cron_name: str
    _logger: logging.Logger

    pool: Registry

    def init(self):
        super().init()
        # not sure why this triggers on abstract models...
        if not self._abstract:
            self.pool.post_init(self._init_cron)

    def _init_cron(self):
        cron_values = {
            'name': self._description,
            'state': 'code',
            'code': "model._process()",
            'interval_number': 24,
            'interval_type': 'hours',
            'numbercall': -1,
        }
        if c := self.env.ref(self._cron_name, raise_if_not_found=False):
            c.write(cron_values)
        else:
            c = self.env['ir.cron'].create({
                **cron_values,
                'model_id': self.env['ir.model']._get(self._name).id,
            })
        self.env['ir.model.data']._update_xmlids([{
            'xml_id': self._cron_name,
            'record': c,
        }])

    @api.model_create_multi
    def create(self, vals_list):
        self.env.ref(self._cron_name)._trigger()
        return super().create(vals_list)

    def _process_item(self):
        raise NotImplementedError

    def _process(self):
        item = self.search(self._search_domain(), limit=1)
        if not item:
            return

        try:
            item._process_item()
            item.unlink()
        except Exception:
            self.env.cr.rollback()
            self._logger.exception("Error while processing %s%s", item, self._failure_log_trailer())
            if item._on_failure():
                item.unlink()
        self.env.ref(self._cron_name)._trigger()

    def _on_failure(self) -> bool:
        _, e, _ = sys.exc_info()
        self._message_log(body=f"Error while processing: {e}")
        return True

    def _failure_log_trailer(self) -> str:
        return ""

    def _search_domain(self):
        return []

class Retryable(models.BaseModel):
    _name = 'runbot_merge.queue.retryable'
    _inherit = ['runbot_merge.queue']
    _order = 'sequence, retry_after, id'
    # retry every 10mn for 2h
    RETRY_LIMIT = 12
    RETRY_DELAY = 10

    sequence = fields.Integer(default=0, required=True, tracking=True)
    retry_after = fields.Datetime(default=fields.Datetime.now, required=True, tracking=True)
    retry_after_relative = fields.Char(compute="_compute_retry_after_relative")
    disabled = fields.Boolean(compute='_compute_retry_after_relative')

    def _init_cron(self):
        super()._init_cron()
        model_id = self.env['ir.model']._get(self._name).id
        if self.env['ir.actions.server'].search([
            ('model_id', '=', model_id),
            ('code', '=', 'records.action_reset()'),
        ]):
            return

        self.env['ir.actions.server'].create({
            'name': "Reset Retries",
            'model_id': model_id,
            'binding_model_id': model_id,
            'binding_type': 'action',
            'state': 'code',
            'code': 'records.action_reset()',
        })

    def write(self, vals):
        if not self:
            return True

        if retry := vals.get('retry_after'):
            minseq = vals.get('sequence') or min(r.sequence for r in self)
            if minseq < self.RETRY_LIMIT:
                self.env.ref(self._cron_name)\
                    ._trigger(fields.Datetime.to_datetime(retry))
        return super().write(vals)

    def _search_domain(self):
        return [
            *super()._search_domain(),
            ('sequence', '<', self.RETRY_LIMIT),
            ('retry_after', '<', datetime.now()),
        ]

    def _on_failure(self) -> bool:
        super()._on_failure()
        self.sequence += 1
        self.retry_after = datetime.now() + timedelta(minutes=self.RETRY_DELAY)
        if self.sequence >= self.RETRY_LIMIT:
            # notify as odoobot to make sure every relevant user always
            # gets notified even if this latch is triggered through a user
            # updating a record
            self.with_user(1).message_notify(
                body=f"Cannot process, disabling.",
                partner_ids=self.env.ref('runbot_merge.group_admin').users.partner_id.ids,
            )
        return False

    def _failure_log_trailer(self) -> str:
        return f" (attempt {self.sequence + 1} / {self.RETRY_LIMIT})"

    @api.depends('retry_after', 'sequence')
    @api.depends_context('lang')
    def _compute_retry_after_relative(self):
        now = fields.Datetime.now()
        for t in self:
            t.disabled = False
            if t.sequence >= self.RETRY_LIMIT:
                t.retry_after_relative = "N/A"
                t.disabled = True
            elif t.retry_after <= now:
                t.retry_after_relative = ""
            else:
                t.retry_after_relative = format_timedelta(
                    t.retry_after - now,
                    locale=self.env.lang or self.env.user.lang or 'en_US',
                )

    def action_reset(self):
        self.sequence = 0
        cron = self.env.ref(self._cron_name)
        retry_now = False
        now = datetime.now()
        for t in self:
            retry = fields.Datetime.to_datetime(t.retry_after)
            if t.retry_after > now:
                cron._trigger(retry)
            elif not retry_now:
                cron._trigger()
                retry_now = True

    def _get_view(self, *args, **kwargs):
        arch: _Element
        view: models.Model
        arch, view = super()._get_view(*args, **kwargs)
        if arch.tag == 'form' and arch.find('.//field[@name = "message_ids"]') is None:
            arch.append(
                E.div(
                    {'class': 'oe_chatter'},
                    E.field(name="message_follower_ids", widget="mail_followers"),
                    E.field(name="message_ids", widget="mail_thread"),
                )
            )
        if arch.tag == 'tree' and arch.find('field[@name = "disabled"]') is None:
            arch.insert(0, E.field(name="disabled"))
            arch.append(E.field(name='sequence', string="attempt"))
            arch.append(E.field(name='retry_after_relative', string="next in", invisible="disabled"))
        return arch, view
