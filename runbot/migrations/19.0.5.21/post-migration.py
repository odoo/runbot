import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # errors used to be shared between projects: take the project of the first build they were seen in
    cr.execute("""
        UPDATE runbot_build_error error
           SET project_id = params.project_id
          FROM runbot_build build
          JOIN runbot_build_params params ON params.id = build.params_id
         WHERE build.id = error.first_seen_build_id
           AND error.project_id IS NULL
    """)
    _logger.info('Set the project of %s errors from their builds', cr.rowcount)

    # errors never seen in a build default to the first project
    cr.execute("SELECT id FROM runbot_project ORDER BY sequence, id LIMIT 1")
    if row := cr.fetchone():
        cr.execute("UPDATE runbot_build_error SET project_id = %s WHERE project_id IS NULL", [row[0]])
        _logger.info('Set project %s on %s errors without builds', row[0], cr.rowcount)

    # contents always belong to the project of their error
    cr.execute("""
        UPDATE runbot_build_error_content content
           SET project_id = error.project_id
          FROM runbot_build_error error
         WHERE error.id = content.error_id
           AND content.project_id IS NULL
    """)
    _logger.info('Set the project of %s error contents from their error', cr.rowcount)
