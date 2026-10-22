"""
- delete old unreferenced commits
- clean up log_access columns
- compress statuses
"""
import logging
import zlib

from psycopg2.extras import execute_values

from odoo.sql_db import db_connect, close_all
from odoo.tools.misc import file_open

_logger = logging.getLogger('odoo.addons.runbot_merge.migrations.17.0.1.23')
COMPRESSION_BATCH_LOG = 10000

def migrate(cr, _version):
    cr.execute("SELECT pg_size_pretty(pg_total_relation_size('runbot_merge_commit'))")
    _logger.info("runbot_merge_commit size: %s", *cr.fetchone())

    cr.execute("ALTER TABLE runbot_merge_commit SET (autovacuum_enabled = false, toast.autovacuum_enabled = false)")
    cr.commit()

    cr.execute(
        """
        DELETE FROM runbot_merge_commit c
        WHERE write_date < now() - interval '1 month'
          AND NOT EXISTS (SELECT 1 FROM runbot_merge_pull_requests p WHERE p.head = c.sha)
          AND NOT EXISTS (SELECT 1 FROM runbot_merge_stagings_heads sh WHERE sh.commit_id = c.id)
          AND NOT EXISTS (SELECT 1 FROM runbot_merge_stagings_commits sc WHERE sc.commit_id = c.id)
        """
    )
    _logger.info("cleanup old commits done")

    cr.execute(
        """
        UPDATE runbot_merge_commit
            SET create_date = write_date
            WHERE create_date IS NULL OR create_date > write_date;
        ALTER TABLE runbot_merge_commit
            DROP COLUMN create_uid,
            DROP COLUMN write_uid,
            DROP COLUMN write_date,
            ADD COLUMN statuses_compressed bytea,
            -- disable TOAST compression since values are already compressed
            ALTER COLUMN statuses_compressed SET STORAGE EXTERNAL;
        """
    )
    _logger.info("cleanup log_access columns done")

    # execute_values pulls lazily from the iterable parameter, so perform the
    # transformation in a generator
    def updates():
        with file_open('runbot_merge/data/runbot_merge.commit.statuses.v1.zdict', 'rb') as f:
            compressor = zlib.compressobj(level=9, memLevel=9, wbits=-15, zdict=f.read())

        cr.execute("SELECT id, statuses FROM runbot_merge_commit")
        sts = cr.fetchall()
        for i, (cid, st) in enumerate(sts):
            if i and ((i % COMPRESSION_BATCH_LOG) == 0):
                _logger.info("processing %s/%s", i, len(sts))

            if (st or '{}') == '{}':
                continue

            c = compressor.copy()
            yield cid, c.compress(st.encode()) + c.flush()

    cr.execute_values(
        """
        UPDATE runbot_merge_commit c
        SET statuses_compressed = t.statuses
        FROM (values %s) as t(id, statuses)
        WHERE c.id = t.id
        """,
        updates(),
        page_size=1000,
    )
    cr.execute("ALTER TABLE runbot_merge_commit DROP COLUMN statuses")
    _logger.info("compression of statuses done")

    cr.commit()
    cr._cnx.set_session(autocommit=True)
    cr.execute("vacuum (full, analyze, verbose) runbot_merge_commit")
    cr.execute("ALTER TABLE runbot_merge_commit RESET (autovacuum_enabled, toast.autovacuum_enabled)")
    cr.execute("SELECT pg_size_pretty(pg_total_relation_size('runbot_merge_commit'))")
    _logger.info("runbot_merge_commit size: %s", *cr.fetchone())
    cr._cnx.set_session(autocommit=False)
