{
    'name': 'merge bot',
    'version': '1.8',
    'depends': ['contacts', 'website'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',

        'data/merge_cron.xml',
        'models/crons/git_maintenance.xml',
        'models/crons/cleanup_scratch_branches.xml',
        'data/runbot_merge.pull_requests.feedback.template.csv',
        'views/res_partner.xml',
        'views/runbot_merge_project.xml',
        'views/mergebot.xml',
        'views/queues.xml',
        'views/configuration.xml',
        'views/templates.xml',
        'models/project_freeze/views.xml',
        'models/staging_cancel/views.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'runbot_merge/static/scss/runbot_merge.scss',
        ],
        'web.assets_backend': [
            'runbot_merge/static/scss/runbot_merge_backend.scss',
            'runbot_merge/static/project_freeze/index.js',
        ],
    },
    'post_load': 'enable_sentry',
    'pre_init_hook': '_check_citext',
    'license': 'LGPL-3',
}
