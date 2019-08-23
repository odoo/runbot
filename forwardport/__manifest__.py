# -*- coding: utf-8 -*-
{
    'name': 'forward port bot',
    'summary': "A port which forward ports successful PRs.",
    'depends': ['runbot_merge'],
    'data': [
        'data/security.xml',
        'data/crons.xml',
        'data/views.xml',
    ],
}
