
def migrate(cr, version):
    # Build errors with test_tags are considered disabled
    cr.execute("""
               UPDATE runbot_build_error
               SET state = 'disabled'
               WHERE test_tags IS NOT NULL AND active IS TRUE
    """)
    # Archived build errors are considered solved
    # Note: archived records with test-tags are considered solved too
    cr.execute("""
               UPDATE runbot_build_error
               SET state = 'solved'
               WHERE active IS FALSE
    """)
