def migrate(cr, version):
    """ copy required status filters from an m2m to branches to a domain
    """
    cr.execute("""
    ALTER TABLE runbot_merge_repository_status
    ADD COLUMN branch_filter varchar
    """)
    cr.execute('''
    SELECT status_id, array_agg(branch_id)
    FROM runbot_merge_repository_status_branch
    GROUP BY status_id
    ''')
    for st, brs in cr.fetchall():
        cr.execute("""
        UPDATE runbot_merge_repository_status
        SET branch_filter = %s
        WHERE id = %s
        """, [
            repr([('id', 'in', brs)]),
            st
        ])
    cr.execute("DROP TABLE runbot_merge_repository_status_branch")
