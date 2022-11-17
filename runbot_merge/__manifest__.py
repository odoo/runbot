{
    'name': 'merge bot',
    'version': '1.7',
    'depends': ['contacts', 'website'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',

        'data/merge_cron.xml',
        'views/res_partner.xml',
        'views/runbot_merge_project.xml',
        'views/mergebot.xml',
        'views/queues.xml',
        'views/configuration.xml',
        'views/templates.xml',
        'models/project_freeze/views.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'runbot_merge/static/scss/runbot_merge.scss',
        ],
        'web.assets_backend': [
            'runbot_merge/static/project_freeze/index.js',
        ],
    },
    'post_load': 'enable_sentry',
    'pre_init_hook': '_check_citext',
    'license': 'LGPL-3',
}
