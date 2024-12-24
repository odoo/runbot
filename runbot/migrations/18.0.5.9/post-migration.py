def migrate(cr, version):
    cr.execute("""
        WITH helper AS
        (
            SELECT   v.id,
            (
                SELECT   v2.id
                FROM     runbot_version v2
                WHERE    Coalesce(v2.SEQUENCE, 9999) <= Coalesce(v.SEQUENCE, 9999)
                AND      v2.number < v.number
                ORDER BY v2.SEQUENCE DESC,
                        v2.number DESC limit 1 ) AS v_excluded
        FROM     runbot_version v
        ORDER BY v.SEQUENCE DESC,
            v.NUMBER DESC )
        UPDATE runbot_build_error
        SET    tags_min_version_excluded_id = h.v_excluded
        FROM   helper h
        WHERE h.id = tags_min_version_id;
        """)
    cr.execute("""ALTER TABLE runbot_build_error DROP COLUMN tags_min_version_id;""")
