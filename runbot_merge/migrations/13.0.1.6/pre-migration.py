def migrate(cr, version):
    """ Status overrides: o2m -> m2m
    """
    # create link table
    cr.execute('''
    CREATE TABLE res_partner_res_partner_override_rel (
        res_partner_id integer not null references res_partner (id) ON DELETE CASCADE,
        res_partner_override_id integer not null references res_partner_override (id) ON DELETE CASCADE,
        primary key (res_partner_id, res_partner_override_id)
    )
    ''')
    cr.execute('''
    CREATE UNIQUE INDEX ON res_partner_res_partner_override_rel
        (res_partner_override_id, res_partner_id)
    ''')

    # deduplicate override rights and insert into link table
    cr.execute('SELECT array_agg(id), array_agg(partner_id)'
               ' FROM res_partner_override GROUP BY repository_id, context')
    links = {}
    duplicants = set()
    for [keep, *drops], partners in cr.fetchall():
        links[keep] = partners
        duplicants.update(drops)
    for override_id, partner_ids in links.items():
        for partner_id in partner_ids:
            cr.execute('INSERT INTO res_partner_res_partner_override_rel (res_partner_override_id, res_partner_id)'
                       ' VALUES (%s, %s)', [override_id, partner_id])
    # drop dups
    cr.execute('DELETE FROM res_partner_override WHERE id = any(%s)', [list(duplicants)])

    # remove old partner field
    cr.execute('ALTER TABLE res_partner_override DROP COLUMN partner_id')
    # add constraint to overrides
    cr.execute('CREATE UNIQUE INDEX res_partner_override_unique ON res_partner_override '
               '(context, coalesce(repository_id, 0))')
