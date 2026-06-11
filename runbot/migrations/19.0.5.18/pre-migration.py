def migrate(cr, version):
    cr.execute("ALTER TABLE runbot_bundle RENAME COLUMN has_pr TO has_active_pr;")
    cr.execute("COMMENT ON COLUMN runbot_bundle.has_active_pr IS 'Has Active PR';")
