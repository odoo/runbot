# -*- coding: utf-8 -*-
{
    'name': "runbot",
    'summary': "Runbot",
    'description': "Runbot for Odoo 11.0",
    'author': "Odoo SA",
    'website': "http://runbot.odoo.com",
    'category': 'Website',
    'version': '2.0',
    'depends': ['website', 'base'],
    'data': [
        'security/runbot_security.xml',
        'security/ir.model.access.csv',
        'security/ir.rule.csv',
        'views/repo_views.xml',
        'views/branch_views.xml',
        'views/build_views.xml',
        'views/res_config_settings_views.xml',
        'templates/frontend.xml',
        'templates/build.xml',
        'templates/assets.xml',
        'templates/nginx.xml',
        'data/runbot_cron.xml'
    ],
}
