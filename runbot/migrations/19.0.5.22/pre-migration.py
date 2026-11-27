def migrate(cr, version):
    cr.execute("ALTER TABLE runbot_build_config_step RENAME COLUMN enable_log_db TO collect_logs;")
    cr.execute("COMMENT ON COLUMN runbot_build_config_step.collect_logs IS 'Collect logs';")
