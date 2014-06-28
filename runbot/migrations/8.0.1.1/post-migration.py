# -*- encoding: utf-8 -*-


from openerp import SUPERUSER_ID
from openerp.modules.registry import RegistryManager


def get_legacy_name(original_name, version):
    return 'legacy_%s_%s' % (version.replace('.', '_'), original_name)


def m2o_to_x2m(cr, model, table, field, source_field):
    cr.execute('SELECT id, %(field)s '
               'FROM %(table)s '
               'WHERE %(field)s is not null' % {
                   'table': table,
                   'field': source_field,
               })
    for row in cr.fetchall():
        model.write(cr, SUPERUSER_ID, row[0], {field: [(4, row[1])]})


def migrate(cr, version):
    if not version:
        return
    registry = RegistryManager.get(cr.dbname)
    m2o_to_x2m(
        cr,
        registry['runbot.repo'],
        'runbot_repo',
        'dependency_ids',
        get_legacy_name('fallback_id', version),
    )
