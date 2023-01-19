def migrate(cr, version):
    """ Add a dummy detach reason to detached PRs.
    """
    cr.execute(
        "ALTER TABLE runbot_merge_pull_requests"
        " ADD COLUMN detach_reason varchar"
    )
    cr.execute(
        "UPDATE runbot_merge_pull_requests"
        " SET detach_reason = 'unknown'"
        " WHERE source_id IS NOT NULL AND parent_id IS NULL")
