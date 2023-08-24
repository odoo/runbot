from psycopg2.extras import execute_values


def migrate(cr, version):
    # Drop all legacy style "previous failures": this is for PRs
    # several years old so almost certainly long irrelevant, and it
    # allows removing the workaround for them. Legacy style has the
    # `state`, `target`, `description` keys at the toplevel while new
    # style is like commit statuses, with the contexts at the toplevel
    # and the status info below.
    cr.execute("""
UPDATE runbot_merge
   SET previous_failure = '{}'
 WHERE previous_failure::jsonb ? 'state'
""")

    # Getting this into postgres native manipulations seems a bit too
    # complicated, and not really necessary.
    cr.execute("""
SELECT id, statuses::json
  FROM runbot_merge_commit
 WHERE jsonb_path_match(statuses::jsonb, '$.*.type() != "object"')
""")
    updated = [
        (id, {
            k: {'state': r, 'target_url': None, 'description': None}
            for k, r in st.items()
        })
        for id, st in cr.fetchall()
    ]
    execute_values(cr._obj, """
UPDATE runbot_merge_commit c
   SET c.statuses = data.st
  FROM (VALUES %s) AS data (id, st)
 WHERE c.id = data.id
""", updated)
