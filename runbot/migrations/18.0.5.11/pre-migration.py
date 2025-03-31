def migrate(cr, version):
    # Replace dot with _
    # Replace (space)\:\(\)\[\] with nothing
    cr.execute(r"""
    UPDATE runbot_dockerfile
       SET name=REGEXP_REPLACE(
            REPLACE(name, '.', '_'),
            '[ /:\(\)\[\]]', '', 'g'
        );
    """)
