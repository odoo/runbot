def migrate(cr, version):
    cr.execute(
    """
        INSERT INTO runbot_build_error_link(build_id,build_error_id,log_date)
             SELECT runbot_build_id,runbot_build_error_id,runbot_build.create_date as create_date 
               FROM runbot_build_error_ids_runbot_build_rel 
          LEFT JOIN runbot_build ON runbot_build.id = runbot_build_id;
    """)
