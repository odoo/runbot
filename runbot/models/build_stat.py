import datetime
import logging
import math

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

    def _gc_stats(self):
        icp = self.env['ir.config_parameter'].sudo()
        gc_days = int(icp.get_param('runbot.stats_gc_days_non_sticky', default=180))
        if gc_days <= 0:
            return
        batch_size = int(icp.get_param('runbot.stats_gc_batch_size', default=100000))
        last_build_id_param = 'runbot.stats_gc_last_build_id_non_sticky'
        start_id = int(icp.get_param(last_build_id_param, default=0))
        cutoff_date = self.env.cr.now() - datetime.timedelta(days=gc_days)
        cutoff_build_id = self.env['runbot.build'].search([('create_date', '>=', cutoff_date)], order='create_date', limit=1).id

        query = """
            WITH old_builds AS (
                SELECT id, split_part(parent_path, '/', 1)::int AS top_id
                FROM runbot_build
                WHERE id >= %(start_id)s AND id < %(end_id)s AND create_date < %(cutoff_date)s
            ), top_builds AS (
                SELECT DISTINCT top_id FROM old_builds
            ), sticky_top_builds AS (
                SELECT top_builds.top_id
                FROM top_builds
                JOIN runbot_batch_slot slot ON slot.build_id = top_builds.top_id
                JOIN runbot_batch batch ON batch.id = slot.batch_id
                JOIN runbot_bundle bundle ON bundle.id = batch.bundle_id AND bundle.sticky
                UNION
                SELECT top_builds.top_id
                FROM top_builds
                JOIN runbot_build_link link ON link.child_id = top_builds.top_id
                JOIN runbot_build parent ON parent.id = link.parent_id
                JOIN runbot_batch_slot slot ON slot.build_id = split_part(parent.parent_path, '/', 1)::int
                JOIN runbot_batch batch ON batch.id = slot.batch_id
                JOIN runbot_bundle bundle ON bundle.id = batch.bundle_id AND bundle.sticky
            ), batch_top_builds AS (
                SELECT top_builds.top_id
                FROM top_builds
                WHERE EXISTS (SELECT 1 FROM runbot_batch_slot slot WHERE slot.build_id = top_builds.top_id)
                OR EXISTS (SELECT 1 FROM runbot_build_link link WHERE link.child_id = top_builds.top_id)
            )
            DELETE FROM runbot_build_stat stat
            USING old_builds
            WHERE stat.build_id = old_builds.id
            AND old_builds.top_id IN (SELECT top_id FROM batch_top_builds)
            AND old_builds.top_id NOT IN (SELECT top_id FROM sticky_top_builds)
        """

        removed_count = 0
        time_left = True
        while start_id < cutoff_build_id and time_left:
            end_id = min(start_id + batch_size, cutoff_build_id)
            self.env.cr.execute(query, {'start_id': start_id, 'end_id': end_id, 'cutoff_date': cutoff_date})
            removed_count += self.env.cr.rowcount
            start_id = end_id
            icp.set_param(last_build_id_param, start_id)
            remaining_batches = math.ceil((cutoff_build_id - start_id) / batch_size)
            time_left = self.env['ir.cron']._commit_progress(1, remaining=remaining_batches)
        _logger.info('Removed %s stats older than %s days (up to build %s)', removed_count, gc_days, start_id)
