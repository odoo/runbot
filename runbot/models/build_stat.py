import logging

from odoo import models, fields, api, tools

_logger = logging.getLogger(__name__)


class RunbotBuildStat(models.Model):
    _name = "runbot.build.stat"
    _description = "Statistics"
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

    id = fields.Many2one("runbot.build.stat", readonly=True)
    key = fields.Char("Key", readonly=True)
    value = fields.Float("Value", readonly=True)
    config_step_id = fields.Many2one(
        "runbot.build.config.step", string="Config Step", readonly=True
    )
    config_step_name = fields.Char(String="Config Step name", readonly=True)
    build_id = fields.Many2one("runbot.build", string="Build", readonly=True)
    build_config_id = fields.Many2one("runbot.build.config", string="Config", readonly=True)
    build_name = fields.Char(String="Build name", readonly=True)
    build_parent_path = fields.Char('Build Parent path')
    build_host = fields.Char(string="Host", readonly=True)
    branch_id = fields.Many2one("runbot.branch", string="Branch", readonly=True)
    branch_name = fields.Char(string="Branch name", readonly=True)
    branch_sticky = fields.Boolean(string="Sticky", readonly=True)
    repo_id = fields.Many2one("runbot.repo", string="Repo", readonly=True)
    repo_name = fields.Char(string="Repo name", readonly=True)

    def init(self):
        """ Create SQL view for build stat """
        tools.drop_view_if_exists(self._cr, "runbot_build_stat_sql")
        self._cr.execute(
            """ CREATE VIEW runbot_build_stat_sql AS (
            SELECT
                stat.id AS id,
                stat.key AS key,
                stat.value AS value,
                step.id AS config_step_id,
                step.name AS config_step_name,
                bu.id AS build_id,
                bu.config_id AS build_config_id,
                bu.parent_path AS build_parent_path,
                bu.name AS build_name,
                bu.host AS build_host,
                br.id AS branch_id,
                br.branch_name AS branch_name,
                br.sticky AS branch_sticky,
                repo.id AS repo_id,
                repo.name AS repo_name
            FROM
                runbot_build_stat AS stat
            JOIN
                runbot_build_config_step step ON stat.config_step_id = step.id
            JOIN
                runbot_build bu ON stat.build_id = bu.id
            JOIN
                runbot_branch br ON br.id = bu.branch_id
            JOIN
                runbot_repo repo ON br.repo_id = repo.id
        )"""
        )
