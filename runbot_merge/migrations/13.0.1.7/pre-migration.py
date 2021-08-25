def migrate(cr, version):
    """ Create draft column manually because the v13 orm can't handle the power
    of adding new required columns
    """
    cr.execute("ALTER TABLE runbot_merge_pull_requests"
               " ADD COLUMN draft BOOLEAN NOT NULL DEFAULT false")
