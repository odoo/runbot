# -*- encoding: utf-8 -*-

import glob
import logging
import re

from odoo import models

_logger = logging.getLogger(__name__)


class runbot_build(models.Model):
    _inherit = "runbot.build"

    def _job_05_check_cla(self, build, lock_path, log_path):
        cla_glob = glob.glob(build._path("doc/cla/*/*.md"))
        if cla_glob:
            description = "%s Odoo CLA signature check" % build.author
            mo = re.search('[^ <@]+@[^ @>]+', build.author_email or '')
            state = "failure"
            if mo:
                email = mo.group(0).lower()
                if re.match('.*@(odoo|openerp|tinyerp)\.com$', email):
                    state = "success"
                else:
                    try:
                        cla = ''.join(open(f).read() for f in cla_glob)
                        if cla.lower().find(email) != -1:
                            state = "success"
                    except UnicodeDecodeError:
                        description = 'Invalid CLA encoding (must be utf-8)'
                    _logger.info('CLA build:%s email:%s result:%s', build.dest, email, state)
            status = {
                "state": state,
                "target_url": "https://www.odoo.com/sign-cla",
                "description": description,
                "context": "legal/cla"
            }
            build._log('check_cla', 'CLA %s' % state)
            build.repo_id._github('/repos/:owner/:repo/statuses/%s' % build.name, status, ignore_errors=True)
        # 0 is myself, -1 is everybody else, -2 nothing
        return -2
