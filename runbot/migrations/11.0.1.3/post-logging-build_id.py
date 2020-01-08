# -*- coding: utf-8 -*-

def migrate(cr, version):
    cr.execute("""
        WITH bad(id) AS (
            SELECT split_part(dbname, '-', 1)::integer
              FROM ir_logging
             WHERE dbname ~ '^\d+-.+'
          GROUP BY 1
            EXCEPT
            SELECT id
              FROM runbot_build
        )
        DELETE FROM ir_logging
              WHERE dbname ~ (SELECT CONCAT('^(', string_agg(id::text, '|'::text), ')-.+') FROM bad);

        UPDATE ir_logging
           SET build_id = split_part(dbname, '-', 1)::integer
         WHERE build_id IS NULL
           AND dbname ~ '^\d+-.+';
    """)
