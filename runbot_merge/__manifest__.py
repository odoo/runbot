{
    'name': 'merge bot',
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
}
