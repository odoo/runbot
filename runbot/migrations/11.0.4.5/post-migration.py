# -*- coding: utf-8 -*-


def migrate(cr, version):

    repo_modules = '-auth_ldap,-document_ftp,-base_gengo,-website_gengo,-website_instantclick,-pad,-pad_project,-note_pad,-pos_cache,-pos_blackbox_be,-hw_*,-theme_*,-l10n_*,'
    cr.execute("UPDATE runbot_repo SET modules = CONCAT(%s, modules) WHERE modules_auto = 'all' or modules_auto = 'repo';", (repo_modules,))

    # ceux qui n'ont pas d'étoile on prefix par '-*,'
    cr.execute("SELECT id,install_modules FROM runbot_build_config_step")
    for step_id, install_modules in cr.fetchall():
        install_modules_list = [mod.strip() for mod in (install_modules or '').split(',') if mod.strip()]
        if '*' in install_modules_list:
            install_modules_list.remove('*')
            install_modules = ', '.join(install_modules_list)
        elif install_modules_list:
            install_modules = '-*,%s' % install_modules
        else:
            install_modules = '-*'
        cr.execute("UPDATE runbot_build_config_step SET install_modules = %s WHERE id=%s", (install_modules, step_id))
