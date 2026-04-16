def migrate(cr, version):
    cr.execute("""UPDATE runbot_build set local_result = 'killed' where local_result = 'manually_killed'""")
    cr.execute("""UPDATE runbot_build set global_result = 'killed' where global_result = 'manually_killed'""")
