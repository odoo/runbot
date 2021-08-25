{
    'name': 'merge bot',
    'version': '1.7',
    'depends': ['contacts', 'website'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',

        'data/merge_cron.xml',
        'views/res_partner.xml',
        'views/mergebot.xml',
        'views/templates.xml',
    ],
    'post_load': 'enable_sentry',
    'pre_init_hook': '_check_citext',
    'license': 'LGPL-3',
}
