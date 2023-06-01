# -*- encoding: utf-8 -*-

import glob
import io
import logging
import re

from odoo import models, fields

_logger = logging.getLogger(__name__)


class Step(models.Model):
    _inherit = "runbot.build.config.step"

    job_type = fields.Selection(selection_add=[('cla_check', 'Check cla')], ondelete={'cla_check': 'cascade'})

    def _run_cla_check(self, build, log_path):
        build._checkout()
        cla_glob = glob.glob(build._get_server_commit()._source_path("doc/cla/*/*.md"))
        error = False
        checked = set()
        if cla_glob:
            for commit in build.params_id.commit_ids:
                email = commit.author_email
                if email in checked:
                    continue
                checked.add(email)
                build._log('check_cla', "[Odoo CLA signature](https://www.odoo.com/sign-cla) check for %s (%s) " % (commit.author, email), log_type='markdown')
                mo = re.search('[^ <@]+@[^ @>]+', email or '')
                if mo:
                    email = mo.group(0).lower()
                    if not re.match('.*@(odoo|openerp|tinyerp)\.com$', email):
                        try:
                            cla = ''.join(io.open(f, encoding='utf-8').read() for f in cla_glob)
                            if cla.lower().find(email) == -1:
                                error = True
                                build._log('check_cla', 'Email not found in cla file %s' % email, level="ERROR")
                        except UnicodeDecodeError:
                            error = True
                            build._log('check_cla', 'Invalid CLA encoding (must be utf-8)', level="ERROR")
                else:
                    error = True
                    build._log('check_cla', 'Invalid email format %s' % email, level="ERROR")
        else:
            error = True
            build._log('check_cla', "Missing cla file", level="ERROR")

        if error:
            build.local_result = 'ko'
        elif not build.local_result:
            build.local_result = 'ok'
