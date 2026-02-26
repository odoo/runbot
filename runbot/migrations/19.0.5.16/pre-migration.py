import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""ALTER TABLE runbot_batch ADD COLUMN priority_level integer""")
    cr.execute("""ALTER TABLE runbot_build ADD COLUMN priority_level integer""")
