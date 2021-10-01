# -*- coding: utf-8 -*-

import logging

from collections import defaultdict

from ..common import pseudo_markdown
from odoo import models, fields, tools
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

TYPES = [(t, t.capitalize()) for t in 'client server runbot subbuild link markdown'.split()]


class runbot_event(models.Model):

    _inherit = "ir.logging"
    _order = 'id'

    build_id = fields.Many2one('runbot.build', 'Build', index=True, ondelete='cascade')
    active_step_id = fields.Many2one('runbot.build.config.step', 'Active step', index=True)
    type = fields.Selection(selection_add=TYPES, string='Type', required=True, index=True)
    error_id = fields.Many2one('runbot.build.error', compute='_compute_known_error')  # remember to never store this field

    def init(self):
        parent_class = super(runbot_event, self)
        if hasattr(parent_class, 'init'):
            parent_class.init()

        self._cr.execute("""
CREATE OR REPLACE FUNCTION runbot_set_logging_build() RETURNS TRIGGER AS $runbot_set_logging_build$
BEGIN
  IF (NEW.build_id IS NULL AND NEW.dbname IS NOT NULL AND NEW.dbname != current_database()) THEN
    NEW.build_id := split_part(NEW.dbname, '-', 1)::integer;
    SELECT active_step INTO NEW.active_step_id FROM runbot_build WHERE runbot_build.id = NEW.build_id;
  END IF;
  IF (NEW.build_id IS NOT NULL) AND (NEW.type = 'server') THEN
    DECLARE
        counter INTEGER;
    BEGIN
        UPDATE runbot_build b
            SET log_counter = log_counter - 1
        WHERE b.id = NEW.build_id;
        SELECT log_counter
        INTO counter
        FROM runbot_build
        WHERE runbot_build.id = NEW.build_id;
        IF (counter = 0) THEN
            NEW.message = 'Log limit reached (full logs are still available in the log file)';
            NEW.level = 'SEPARATOR';
            NEW.func = '';
            NEW.type = 'runbot';
            RETURN NEW;
        ELSIF (counter < 0) THEN
                RETURN NULL;
        END IF;
    END;
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

    def _markdown(self):
        """ Apply pseudo markdown parser for message.
        """
        self.ensure_one()
        return pseudo_markdown(self.message)


    def _compute_known_error(self):
        cleaning_regexes = self.env['runbot.error.regex'].search([('re_type', '=', 'cleaning')])
        fingerprints = defaultdict(list)
        for ir_logging in self:
            ir_logging.error_id = False
            if ir_logging.level == 'ERROR' and ir_logging.type == 'server':
                fingerprints[self.env['runbot.build.error']._digest(cleaning_regexes.r_sub('%', ir_logging.message))].append(ir_logging)
        for build_error in self.env['runbot.build.error'].search([('fingerprint', 'in', list(fingerprints.keys()))]):
            for ir_logging in fingerprints[build_error.fingerprint]:
                ir_logging.error_id = build_error.id

class RunbotErrorLog(models.Model):
    _name = 'runbot.error.log'
    _description = "Error log"
    _auto = False
    _order = 'id desc'

    id = fields.Many2one('ir.logging', string='Log', readonly=True)
    name = fields.Char(string='Module', readonly=True)
    message = fields.Text(string='Message', readonly=True)
    summary = fields.Text(string='Summary', readonly=True)
    log_type = fields.Char(string='Type', readonly=True)
    log_create_date = fields.Datetime(string='Log create date', readonly=True)
    func = fields.Char(string='Method', readonly=True)
    path = fields.Char(string='Path', readonly=True)
    line = fields.Char(string='Line', readonly=True)
    build_id = fields.Many2one('runbot.build', string='Build', readonly=True)
    dest = fields.Char(String='Build dest', readonly=True)
    local_state = fields.Char(string='Local state', readonly=True)
    local_result = fields.Char(string='Local result', readonly=True)
    global_state = fields.Char(string='Global state', readonly=True)
    global_result = fields.Char(string='Global result', readonly=True)
    bu_create_date = fields.Datetime(string='Build create date', readonly=True)
    host = fields.Char(string='Host', readonly=True)
    parent_id = fields.Many2one('runbot.build', string='Parent build', readonly=True)
    top_parent_id = fields.Many2one('runbot.build', string="Top parent", readonly=True)
    bundle_ids = fields.Many2many('runbot.bundle', compute='_compute_bundle_id',  search='_search_bundle', string='Bundle', readonly=True)
    sticky = fields.Boolean(string='Bundle Sticky', compute='_compute_bundle_id', search='_search_sticky', readonly=True)
    build_url = fields.Char(compute='_compute_build_url', readonly=True)

    def _compute_repo_short_name(self):
        for l in self:
            l.repo_short_name = '%s/%s' % (l.repo_id.owner, l.repo_id.repo_name)

    def _compute_build_url(self):
        for l in self:
            l.build_url = '/runbot/build/%s' % l.build_id.id

    def action_goto_build(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": "runbot/build/%s" % self.build_id.id,
            "target": "new",
        }

    def _compute_bundle_id(self):
        slots = self.env['runbot.batch.slot'].search([('build_id', 'in', self.mapped('top_parent_id').ids)])
        for l in self:
            l.bundle_ids = slots.filtered(lambda rec: rec.build_id.id == l.top_parent_id.id).batch_id.bundle_id
            l.sticky = any(l.bundle_ids.filtered('sticky'))

    def _search_bundle(self, operator, value):
        query = """
          SELECT id
          FROM runbot_build as build
          WHERE EXISTS(
            SELECT * FROM runbot_batch_slot as slot
            JOIN
              runbot_batch batch ON batch.id = slot.batch_id
            JOIN
              runbot_bundle bundle ON bundle.id = batch.bundle_id
            %s
        """
        if operator in ('ilike', '=', 'in'):
            value = '%%%s%%' % value if operator == 'ilike' else value
            col_name = 'id' if operator == 'in' else 'name'
            where_condition = "WHERE slot.build_id = build.id AND bundle.%s %s any(%%s));" if operator == 'in' else "WHERE slot.build_id = build.id AND bundle.%s %s %%s);"
            operator = '=' if operator == 'in' else operator
            where_condition = where_condition % (col_name, operator)
            query = query % where_condition
            self.env.cr.execute(query, (value,))
            build_ids = [t[0] for t in self.env.cr.fetchall()]
            return [('top_parent_id', 'in', build_ids)]

        raise UserError('Operator `%s` not implemented for bundle search' % operator)

    def search_count(self, args):
       return 4242  # hack to speed up the view

    def _search_sticky(self, operator, value):
        if operator == '=':
            self.env.cr.execute("""
              SELECT id
              FROM runbot_build as build
              WHERE EXISTS(
                SELECT * FROM runbot_batch_slot as slot
                JOIN
                  runbot_batch batch ON batch.id = slot.batch_id
                JOIN
                  runbot_bundle bundle ON bundle.id = batch.bundle_id
                WHERE
                  bundle.sticky = %s AND slot.build_id = build.id);
            """, (value,))
            build_ids = [t[0] for t in self.env.cr.fetchall()]
            return [('top_parent_id', 'in', build_ids)]
        return []

    def _parse_logs(self):
        BuildError = self.env['runbot.build.error']
        return BuildError._parse_logs(self)

    def init(self):
        """ Create an SQL view for ir.logging """
        tools.drop_view_if_exists(self._cr, 'runbot_error_log')
        self._cr.execute(""" CREATE VIEW runbot_error_log AS (
            SELECT
                l.id  AS id,
                l.name  AS name,
                l.message  AS message,
                left(l.message, 50) as summary,
                l.type  AS log_type,
                l.create_date  AS log_create_date,
                l.func  AS func,
                l.path  AS path,
                l.line  AS line,
                bu.id  AS build_id,
                bu.dest AS dest,
                bu.local_state  AS local_state,
                bu.local_result  AS local_result,
                bu.global_state  AS global_state,
                bu.global_result  AS global_result,
                bu.create_date  AS bu_create_date,
                bu.host  AS host,
                bu.parent_id  AS parent_id,
                split_part(bu.parent_path, '/',1)::int AS top_parent_id
            FROM
                ir_logging AS l
            JOIN
                runbot_build bu ON l.build_id = bu.id
            WHERE
                l.level = 'ERROR'
        )""")
