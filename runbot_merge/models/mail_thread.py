from collections import ChainMap

from odoo import models
from odoo.tools.misc import ConstantMapping


class MailThread(models.AbstractModel):
    _inherit = 'mail.thread'

    def _message_compute_author(self, author_id=None, email_from=None, raise_on_email=True):
        if author_id is None and self:
            mta = self.env.cr.precommit.data.get(f'mail.tracking.author.{self._name}', {})
            authors = self.env['res.partner'].union(*(p for r in self if (p := mta.get(r.id))))
            if len(authors) == 1:
                author_id = authors.id
        v = super()._message_compute_author(author_id, email_from, raise_on_email)
        return v

    def _track_set_author(self, author, *, fallback=False):
        """ Set the author of the tracking message. """
        if not self._track_get_fields():
            return
        authors = self.env.cr.precommit.data.setdefault(f'mail.tracking.author.{self._name}', {})
        if fallback:
            details = authors
            if isinstance(authors, ChainMap):
                details = authors.maps[0]
            self.env.cr.precommit.data[f'mail.tracking.author.{self._name}'] = ChainMap(
                details,
                ConstantMapping(author),
            )
        else:
            return super()._track_set_author(author)
