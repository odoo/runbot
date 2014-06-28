# -*- encoding: utf-8 -*-

from openerp import release
import logging

logger = logging.getLogger('upgrade')


def get_legacy_name(original_name, version):
    return 'legacy_%s_%s' % (version.replace('.', '_'), original_name)


def rename_columns(cr, column_spec, version):
    for table, renames in column_spec.iteritems():
        for old, new in renames:
            if new is None:
                new = get_legacy_name(old, version)
            logger.info("table %s, column %s: renaming to %s",
                        table, old, new)
            cr.execute('ALTER TABLE "%s" RENAME "%s" TO "%s"'
                       % (table, old, new,))
            cr.execute('DROP INDEX IF EXISTS "%s_%s_index"'
                       % (table, old))

column_renames = {
    'runbot_repo': [
        ('fallback_id', None)
    ]
}


def migrate(cr, version):
    if not version:
        return
    rename_columns(cr, column_renames, version)
