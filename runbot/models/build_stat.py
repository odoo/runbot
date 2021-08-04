import logging

from odoo import models, fields, api, tools

_logger = logging.getLogger(__name__)


class BuildStat(models.Model):
    _name = "runbot.build.stat"
    _description = "Statistics"
    _log_access = False

    _sql_constraints = [
        (
            "build_config_key_unique",
            "unique (build_id, config_step_id,key)",
            "Build stats must be unique for the same build step",
        )
    ]

    build_id = fields.Many2one("runbot.build", "Build", index=True, ondelete="cascade")
    config_step_id = fields.Many2one(
        "runbot.build.config.step", "Step", index=True, ondelete="cascade"
    )
    key = fields.Char("key", index=True)
    value = fields.Float("Value")

    @api.model
    def _write_key_values(self, build, config_step, key_values):
        if not key_values:
            return self
        build_stats = [
            {
                "config_step_id": config_step.id,
                "build_id": build.id,
                "key": k,
                "value": v,
            }
            for k, v in key_values.items()
        ]
        return self.create(build_stats)


class RunbotBuildStatSql(models.Model):

    _name = "runbot.build.stat.sql"
    _description = "Build stat sql view"
    _auto = False

    bundle_id = fields.Many2one("runbot.bundle", string="Bundle", readonly=True)
    bundle_name = fields.Char(string="Bundle name", readonly=True)
    bundle_sticky = fields.Boolean(string="Sticky", readonly=True)
    batch_id = fields.Many2one("runbot.bundle", string="Batch", readonly=True)
    trigger_id = fields.Many2one("runbot.trigger", string="Trigger", readonly=True)
    trigger_name = fields.Char(string="Trigger name", readonly=True)

    stat_id = fields.Many2one("runbot.build.stat", string="Stat", readonly=True)
    key = fields.Char("Key", readonly=True)
    value = fields.Float("Value", readonly=True)

    config_step_id = fields.Many2one(
        "runbot.build.config.step", string="Config Step", readonly=True
    )
    config_step_name = fields.Char(String="Config Step name", readonly=True)

    build_id = fields.Many2one("runbot.build", string="Build", readonly=True)
    build_config_id = fields.Many2one("runbot.build.config", string="Config", readonly=True)
    build_parent_path = fields.Char('Build Parent path')
    build_host = fields.Char(string="Host", readonly=True)

    def init(self):
        """ Create SQL view for build stat """
        tools.drop_view_if_exists(self._cr, "runbot_build_stat_sql")
        self._cr.execute(
            """ CREATE OR REPLACE VIEW runbot_build_stat_sql AS (
            SELECT
                (stat.id::bigint*(2^32)+bun.id::bigint) AS id,
                stat.id AS stat_id,
                stat.key AS key,
                stat.value AS value,
                step.id AS config_step_id,
                step.name AS config_step_name,
                bu.id AS build_id,
                bp.config_id AS build_config_id,
                bu.parent_path AS build_parent_path,
                bu.host AS build_host,
                bun.id AS bundle_id,
                bun.name AS bundle_name,
                bun.sticky AS bundle_sticky,
                ba.id AS batch_id,
                tr.id AS trigger_id,
                tr.name AS trigger_name
            FROM
                runbot_build_stat AS stat
            JOIN
                runbot_build_config_step step ON stat.config_step_id = step.id
            JOIN
                runbot_build bu ON bu.id = stat.build_id
            JOIN
                runbot_build_params bp ON bp.id =bu.params_id
            JOIN
                runbot_batch_slot bas ON bas.build_id = stat.build_id
            JOIN
                runbot_trigger tr ON tr.id = bas.trigger_id
            JOIN
                runbot_batch ba ON ba.id = bas.batch_id
            JOIN
                runbot_bundle bun ON bun.id = ba.bundle_id
        )"""
        )