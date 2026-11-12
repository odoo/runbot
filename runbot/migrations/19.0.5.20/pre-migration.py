import logging

from odoo.tools.sql import column_exists

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if column_exists(cr, 'runbot_build', 'used_in_base'):
        _logger.info('Column runbot_build.used_in_base already exists, skipping the backfill')
        return

    cr.execute("ALTER TABLE runbot_build ADD COLUMN used_in_base boolean DEFAULT false")
    cr.execute("ALTER TABLE runbot_build ALTER COLUMN used_in_base DROP DEFAULT")

    cr.execute("""
        UPDATE runbot_build build
        SET used_in_base = true
        FROM runbot_batch_slot slot
        JOIN runbot_batch batch ON batch.id = slot.batch_id
        JOIN runbot_bundle bundle ON bundle.id = batch.bundle_id
        WHERE slot.build_id = build.id
        AND bundle.is_base
        AND NOT build.used_in_base
    """)
    _logger.info('Flagged %s top builds used in base', cr.rowcount)

    cr.execute("""
        UPDATE runbot_build build
        SET used_in_base = true
        FROM runbot_build_link link
        JOIN runbot_build parent ON parent.id = link.parent_id
        JOIN runbot_build top ON top.id = NULLIF(split_part(parent.parent_path, '/', 1), '')::int
        WHERE link.child_id = build.id
        AND top.used_in_base
        AND NOT build.used_in_base
    """)
    _logger.info('Flagged %s linked builds used in base', cr.rowcount)

    cr.execute("""
        UPDATE runbot_build build
        SET used_in_base = true
        FROM runbot_build top
        WHERE top.id = NULLIF(split_part(build.parent_path, '/', 1), '')::int
        AND top.used_in_base
        AND NOT build.used_in_base
    """)
    _logger.info('Flagged %s child builds used in base', cr.rowcount)
