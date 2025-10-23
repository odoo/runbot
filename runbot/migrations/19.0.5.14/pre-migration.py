def migrate(cr, version):
    cr.execute('ALTER TABLE runbot_build_params ADD COLUMN IF NOT EXISTS dynamic_config JSONB')
