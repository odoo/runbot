import logging

from odoo import models, fields, api, tools
from ..fields import JsonDictField

_logger = logging.getLogger(__name__)


class BuildStat(models.Model):
    _name = "runbot.build.stat"
    _description = "Statistics"
    _log_access = False

    _build_config_key_unique = models.Constraint(
        'unique (build_id, config_step_id, category, dynamic_step_name)',
        "Build stats must be unique for the same build step",
    )

    build_id = fields.Many2one("runbot.build", "Build", index=True, ondelete="cascade")
    config_step_id = fields.Many2one(
        "runbot.build.config.step", "Step", ondelete="cascade",
    )
    dynamic_step_name = fields.Char("Dynamic Step Name")
    category = fields.Char("Category", index=True)
    values = JsonDictField("Value")
