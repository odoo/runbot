# -*- coding: utf-8 -*-


def migrate(cr, version):
    cr.execute("UPDATE runbot_build SET requested_action='deathrow', local_state='testing' WHERE local_state = 'deathrow'")
