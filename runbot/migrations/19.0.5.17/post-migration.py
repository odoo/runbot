import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE runbot_batch ba
        SET version_id = (
            SELECT version_id
            FROM runbot_bundle bu
            WHERE bu.id = ba.bundle_id
        )
        WHERE ba.bundle_id IS NOT NULL
    """)