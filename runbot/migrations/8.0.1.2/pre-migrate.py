#!/usr/bin/env python
# -*- coding: utf-8 -*-

def migrate(cr, version):
    cr.execute("""SELECT 1
                    FROM information_schema.columns
                   WHERE table_name='runbot_branch'
                     AND column_name='pull_head_name'
               """)
    if not cr.rowcount:
        cr.execute('ALTER TABLE "runbot_branch" ADD COLUMN "pull_head_name" varchar')
