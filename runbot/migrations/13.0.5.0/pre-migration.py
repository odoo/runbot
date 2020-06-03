# -*- coding: utf-8 -*-
import logging
_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # dependency is not correct since it will be all commits. This also free the name for a build dependant on another build params

    # those indexes are improving the branches deletion
    cr.execute('CREATE INDEX ON runbot_branch (defined_sticky);')
    cr.execute('CREATE INDEX ON runbot_build_dependency (closest_branch_id);')

    # Fix duplicate problems
    cr.execute("UPDATE runbot_build SET duplicate_id = null WHERE duplicate_id > id")
    cr.execute("UPDATE runbot_build SET local_state='done' WHERE duplicate_id IS NULL AND local_state = 'duplicate';")
    # Remove builds without a repo
    cr.execute("DELETE FROM runbot_build WHERE repo_id IS NULL")

    cr.execute("DELETE FROM ir_ui_view WHERE id IN (SELECT res_id FROM ir_model_data WHERE name = 'inherits_branch_in_menu' AND module = 'runbot')")

    # Fix branches
    cr.execute("""DELETE FROM runbot_branch WHERE name SIMILAR TO 'refs/heads/\d+' RETURNING id,name;""")  # Remove old bad branches named like PR
    for branch_id, name in cr.fetchall():
        _logger.warning('Deleting branch id %s with name "%s"', branch_id, name)

    cr.execute("""SELECT branch_name,repo_id, count(*) AS nb FROM runbot_branch GROUP BY branch_name,repo_id HAVING count(*) > 1;""")  # Branches with duplicate branch_name in same repo
    for branch_name, repo_id, nb in cr.fetchall():
        cr.execute("""DELETE FROM runbot_branch WHERE (sticky='f' OR sticky IS NULL) AND branch_name=%s and repo_id=%s and name ~ 'refs/heads/.+/.+' RETURNING id,branch_name;""", (branch_name, repo_id))
        for branch_id, branch_name in cr.fetchall():
            _logger.warning('Deleting branch id %s with branch_name "%s"', branch_id, branch_name)

    # Raise in case of buggy PR's
    cr.execute("SELECT id,name FROM runbot_branch WHERE name LIKE 'refs/pull/%' AND pull_head_name is null")
    bad_prs = cr.fetchall()
    if bad_prs:
        for pr in bad_prs:
            _logger.warning('PR with NULL pull_head_name found: %s (%s)', pr[1], pr[0])
        raise RuntimeError("Migration error", "Found %s PR's without pull_head_name" % len(bad_prs))

    # avoid recompute of branch._comput_bundle_id otherwise, it cannot find xml data
    cr.execute('ALTER TABLE runbot_branch ADD COLUMN bundle_id INTEGER;')

    # avoid recompute of pull_head_name wich is emptied during the recompute
    cr.execute('ALTER TABLE runbot_branch ADD COLUMN pull_head_remote_id INTEGER;')

    cr.execute('ALTER TABLE runbot_branch ADD COLUMN is_pr BOOLEAN;')
    cr.execute("""UPDATE runbot_branch SET is_pr = CASE WHEN name like 'refs/pull/%' THEN true ELSE false END;""")

    # delete runbot.repo inehrited views
    cr.execute("DELETE FROM ir_ui_view WHERE inherit_id IN (SELECT id from ir_ui_view WHERE name = 'runbot.repo');")
    return
