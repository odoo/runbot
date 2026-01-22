import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        SELECT to_regclass('public.x_runbot_semgrep_rules');
    """)
    if not cr.fetchone()[0]:
        return

    cr.execute("""SELECT  "x_checker", "x_language", "x_maxver", "x_message", "x_minver", "x_name", "x_rule", "x_severity" FROM x_runbot_semgrep_rules""")
    results = cr.dictfetchall()
    _logger.info('Migrating %d semgrep rules', len(results))
    categories = []
    for result in results:
        categories.append(result['x_checker'])

    category_map = {}
    for category in sorted(set(categories)):
        cr.execute("""
            INSERT INTO runbot_checker_category (name)
            VALUES (%s)
            RETURNING id
        """, (category,))
        category_map[category] = cr.fetchone()[0]

    for result in results:
        cr.execute("""
            INSERT INTO runbot_semgrep_rule (name, category_id, language, max_version_number, min_version_number, message, rule, severity)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            result['x_name'],
            category_map[result['x_checker']],
            result['x_language'],
            result['x_maxver'],
            result['x_minver'],
            result['x_message'],
            result['x_rule'],
            result['x_severity'],
        ))
