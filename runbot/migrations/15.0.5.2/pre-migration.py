def migrate(cr, version):
    cr.execute("DROP TRIGGER IF EXISTS runbot_new_logging ON ir_logging")
    cr.execute("DROP FUNCTION IF EXISTS runbot_set_logging_build")
