# -*- coding: utf-8 -*-

import logging

from odoo import models, fields, api

_logger = logging.getLogger(__name__)

TYPES = [(t, t.capitalize()) for t in 'client server runbot subbuild'.split()]


class runbot_event(models.Model):

    _inherit = "ir.logging"
    _order = 'id'

    build_id = fields.Many2one('runbot.build', 'Build', index=True, ondelete='cascade')
    type = fields.Selection(TYPES, string='Type', required=True, index=True)

    @api.model_cr
    def init(self):
        parent_class = super(runbot_event, self)
        if hasattr(parent_class, 'init'):
            parent_class.init()

        self._cr.execute("""
CREATE OR REPLACE FUNCTION runbot_set_logging_build() RETURNS TRIGGER AS $runbot_set_logging_build$
BEGIN
  IF (NEW.build_id IS NULL AND NEW.dbname IS NOT NULL AND NEW.dbname != current_database()) THEN
    NEW.build_id := split_part(NEW.dbname, '-', 1)::integer;
  END IF;
  IF (NEW.build_id IS NOT NULL AND UPPER(NEW.level) NOT IN ('INFO', 'SEPARATOR')) THEN
    BEGIN
        UPDATE runbot_build b
            SET triggered_result = CASE WHEN UPPER(NEW.level) = 'WARNING' THEN 'warn'
                                        ELSE 'ko'
                                   END
        WHERE b.id = NEW.build_id;
    END;
  END IF;
RETURN NEW;
END;
$runbot_set_logging_build$ language plpgsql;

DROP TRIGGER IF EXISTS runbot_new_logging ON ir_logging;
CREATE TRIGGER runbot_new_logging BEFORE INSERT ON ir_logging
FOR EACH ROW EXECUTE PROCEDURE runbot_set_logging_build();

        """)
