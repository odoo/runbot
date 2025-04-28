def migrate(cr, version):
    cr.execute("UPDATE runbot_build set local_state='killed' where local_state='manually_killed')
    cr.execute("UPDATE runbot_build set global_state='killed' where global_state='manually_killed')
