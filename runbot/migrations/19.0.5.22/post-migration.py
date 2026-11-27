def migrate(cr, version):
    cr.execute("DELETE FROM ir_config_parameter WHERE key = 'runbot.runbot_db_template'")
