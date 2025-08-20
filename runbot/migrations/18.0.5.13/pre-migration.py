def migrate(cr, version):
    cr.execute('ALTER TABLE runbot_build_error_link ADD COLUMN batch_id INT')
    cr.execute('ALTER TABLE runbot_build_error_link ADD COLUMN batch_date TIMESTAMP WITHOUT TIME ZONE')
    cr.execute('''
      UPDATE runbot_build_error_link SET batch_id = batch.id, batch_date = batch.create_date
        FROM runbot_batch as batch
        JOIN runbot_build build on build.create_batch_id = batch.id
        JOIN runbot_build_error_link as link on link.build_id = build.id
    ''')