# -*- coding: utf-8 -*-
{
    'name': 'forward port bot',
    'author': "Odoo SA",
    'version': '1.4',
    'summary': "A port which forward ports successful PRs.",
    'depends': ['runbot_merge'],
    'data': [
        'data/crons.xml',
        'data/views.xml',
        'data/queues.xml',
        'ir.access.csv',
    ],
    'license': 'LGPL-3',
}
