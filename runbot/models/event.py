# -*- coding: utf-8 -*-

import logging

from odoo import models, fields, api, tools

_logger = logging.getLogger(__name__)

TYPES = [(t, t.capitalize()) for t in 'client server runbot subbuild link'.split()]


class runbot_event(models.Model):

    _inherit = "ir.logging"
    _order = 'id'

    build_id = fields.Many2one('runbot.build', 'Build', index=True, ondelete='cascade')
    active_step_id = fields.Many2one('runbot.build.config.step', 'Active step', index=True)
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


class RunbotErrorLog(models.Model):
    _name = "runbot.error.log"
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
    bu_name = fields.Char(String='Build name', readonly=True)
    dest = fields.Char(String='Build dest', readonly=True)
    local_state = fields.Char(string='Local state', readonly=True)
    local_result = fields.Char(string='Local result', readonly=True)
    global_state = fields.Char(string='Global state', readonly=True)
    global_result = fields.Char(string='Global result', readonly=True)
    bu_create_date = fields.Datetime(string='Build create date', readonly=True)
    committer = fields.Char(string='committer', readonly=True)
    author = fields.Char(string='Author', readonly=True)
    host = fields.Char(string='Host', readonly=True)
    config_id = fields.Many2one('runbot.build.config', string='Config', readonly=True)
    parent_id = fields.Many2one('runbot.build', string='Parent build', readonly=True)
    hidden = fields.Boolean(string='Hidden', readonly=True)
    branch_id = fields.Many2one('runbot.branch', string='Branch', readonly=True)
    branch_name = fields.Char(string='Branch name', readonly=True)
    branch_sticky = fields.Boolean(string='Sticky', readonly=True)
    repo_id = fields.Many2one('runbot.repo', string='Repo', readonly=True)
    repo_name = fields.Char(string='Repo name', readonly=True)
    repo_short_name = fields.Char(compute='_compute_repo_short_name', readonly=True)
    build_url = fields.Char(compute='_compute_build_url', readonly=True)

    def _compute_repo_short_name(self):
        for l in self:
            l.repo_short_name = '/'.join(l.repo_id.base.split('/')[-2:])

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

    def _parse_logs(self):
        BuildError = self.env['runbot.build.error']
        BuildError._parse_logs(self)


    @api.model_cr
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
                bu.name AS bu_name,
                bu.dest AS dest,
                bu.local_state  AS local_state,
                bu.local_result  AS local_result,
                bu.global_state  AS global_state,
                bu.global_result  AS global_result,
                bu.create_date  AS bu_create_date,
                bu.committer  AS committer,
                bu.author  AS author,
                bu.host  AS host,
                bu.config_id  AS config_id,
                bu.parent_id  AS parent_id,
                bu.hidden  AS hidden,
                br.id  AS branch_id,
                br.branch_name  AS branch_name,
                br.sticky AS branch_sticky,
                re.id  AS repo_id,
                re.name  AS repo_name
            FROM
                ir_logging AS l
            JOIN
                runbot_build bu ON l.build_id = bu.id
            JOIN
                runbot_branch br ON br.id = bu.branch_id
            JOIN
                runbot_repo re ON br.repo_id = re.id
            WHERE
                l.level = 'ERROR'
        )""")
