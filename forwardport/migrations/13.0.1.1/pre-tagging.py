def migrate(cr, version):
    cr.execute("delete from ir_model where model = 'forwardport.tagging'")
