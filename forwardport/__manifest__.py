# -*- coding: utf-8 -*-
{
    'name': 'forward port bot',
    'author': "Odoo SA",
    'version': '1.4',
    'summary': "A port which forward ports successful PRs.",
    'depends': ['runbot_merge'],
    'data': [
        'data/security.xml',
        'data/crons.xml',
        'data/views.xml',
        'data/queues.xml',
    ],
    'license': 'LGPL-3',
}
