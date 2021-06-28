# -*- coding: utf-8 -*-

def migrate(cr, _version):
    cr.execute('ALTER TABLE runbot_branch ADD COLUMN draft BOOLEAN')
