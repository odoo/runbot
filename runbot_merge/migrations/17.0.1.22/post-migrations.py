def migrate(cr, _version):
    cr.execute(
        "SELECT github_token, fp_github_token, array_agg(id)"
        " FROM runbot_merge_project"
        " GROUP BY github_token, fp_github_token"
    )
    for ght, fpt, projects in cr.fetchall():
        cr.execute(
            """
INSERT INTO runbot_merge_github_connector
    (create_uid, write_uid, create_date, write_date, method, main_pat, fp_pat, name)
VALUES (1, 1, now() at time zone 'utc', now() at time zone 'utc', 'pat', %s, %s, 'original patterns')
RETURNING id
            """,
            [ght, fpt],
        )
        connector_id = cr.fetchone()[0]
        cr.execute(
            "UPDATE runbot_merge_project SET github_id = %s WHERE id = ANY(%s)",
            [connector_id, projects],
        )
    cr.execute("ALTER TABLE runbot_merge_project DROP github_token, DROP fp_github_token")