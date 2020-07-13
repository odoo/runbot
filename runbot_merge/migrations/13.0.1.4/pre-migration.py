import re

def migrate(cr, version):
    """ required_statuses is now a separate object in its own table
    """
    # apparently the DDL has already been updated but the reflection gunk
    cr.execute("""
        DELETE FROM ir_model_fields
        WHERE model = 'runbot_merge.pull_requests.tagging'
          AND name in ('state_from', 'state_to')
    """)

    cr.execute("""
    CREATE TABLE runbot_merge_repository_status (
        id SERIAL NOT NULL PRIMARY KEY,
        context VARCHAR NOT NULL,
        repo_id INTEGER NOT NULL REFERENCES runbot_merge_repository (id) ON DELETE CASCADE,
        prs BOOLEAN,
        stagings BOOLEAN
    )
    """)
    cr.execute("""
    CREATE TABLE runbot_merge_repository_status_branch (
        status_id INTEGER NOT NULL REFERENCES runbot_merge_repository_status (id) ON DELETE CASCADE,
        branch_id INTEGER NOT NULL REFERENCES runbot_merge_branch (id) ON DELETE CASCADE
    )
    """)

    cr.execute('select id, required_statuses from runbot_merge_repository')
    for repo, statuses in cr.fetchall():
        for st in re.split(r',\s*', statuses):
            cr.execute("""
                INSERT INTO runbot_merge_repository_status (context, repo_id, prs, stagings)
                VALUES (%s, %s, true, true)
            """, [st, repo])
