def migrate(cr, version):
    cr.execute("DELETE FROM ir_model_data WHERE module = 'runbot' AND  model = 'runbot.bundle' and name in ('bundle_master', 'bundle_dummy')")
