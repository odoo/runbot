def migrate(cr, _version):
    """Update all the acls from data to be be noupdate so -u stops breaking
    customizations
    """
    cr.execute("""
UPDATE ir_model_data
   SET noupdate = true
 WHERE module = 'runbot_merge'
   AND model = 'runbot_merge.acls'
""")

