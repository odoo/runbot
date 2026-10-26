def migrate(cr, _version):
    cr.execute('select id, staging_pattern, staging_sub_pattern from runbot_merge_project')
    for pid, sp, ssp in cr.fetchall():
        ssp = sp % {
            'target': '%(target)s',
            'stage': '%(stage)s',
            'sub': ssp,
        }
        sp = sp %  {
            'target': '%(target)s',
            'stage': '%(stage)s',
            'sub': '',
        }
        cr.execute('update runbot_merge_project set staging_pattern=%s, staging_sub_pattern=%s where id=%s', (sp, ssp, pid))