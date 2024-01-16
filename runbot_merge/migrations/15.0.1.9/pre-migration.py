from psycopg2.extras import execute_values


def migrate(cr, version):
    # Drop all legacy style "previous failures": this is for PRs
    # several years old so almost certainly long irrelevant, and it
    # allows removing the workaround for them. Legacy style has the
    # `state`, `target`, `description` keys at the toplevel while new
    # style is like commit statuses, with the contexts at the toplevel
    # and the status info below.
    cr.execute("""
UPDATE runbot_merge_pull_requests
   SET previous_failure = '{}'
 WHERE previous_failure::jsonb ? 'state'
""")

    cr.execute("""
WITH new_statuses (id, statuses) AS (
    SELECT id, json_object_agg(
        key,
        CASE WHEN jsonb_typeof(value) = 'string'
            THEN jsonb_build_object('state', value, 'target_url', null, 'description', null)
        ELSE value
        END
    ) AS statuses
    FROM runbot_merge_commit
    CROSS JOIN LATERAL jsonb_each(statuses::jsonb) s
    WHERE jsonb_path_match(statuses::jsonb, '$.*.type() != "object"')
    GROUP BY id
)
UPDATE runbot_merge_commit SET statuses = new_statuses.statuses FROM new_statuses WHERE runbot_merge_commit.id = new_statuses.id
    """)
