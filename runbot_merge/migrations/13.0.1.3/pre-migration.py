def migrate(cr, version):
    cr.execute("DROP INDEX runbot_merge_unique_gh_login")
