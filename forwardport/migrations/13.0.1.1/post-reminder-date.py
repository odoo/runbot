def migrate(cr, version):
    """ Set the merge_date field to the current write_date, and reset
    the backoff to its default so we reprocess old PRs properly.
    """
    cr.execute("""
        UPDATE runbot_merge_pull_requests
           SET merge_date = write_date,
               reminder_backoff_factor = -4
         WHERE state = 'merged'
    """)
