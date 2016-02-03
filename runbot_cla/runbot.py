# -*- encoding: utf-8 -*-

import glob
import logging
import re

import openerp
from openerp.tools import ustr

_logger = logging.getLogger(__name__)

class runbot_build(openerp.models.Model):
    _inherit = "runbot.build"

    def job_05_check_cla(self, cr, uid, build, lock_path, log_path):
        cla_glob = glob.glob(build.path("doc/cla/*/*.md"))
        if cla_glob:
            cla = ''.join(open(f).read() for f in cla_glob)
            cla = ustr(cla.lower())
            mo = re.search('[^ <@]+@[^ @>]+', build.author_email or '')
            state = "failure"
            if mo:
                email = mo.group(0).lower()
                if re.match('.*@(odoo|openerp|tinyerp)\.com$', email):
                    state = "success"
                if cla.find(email) != -1:
                    state = "success"
                _logger.info('CLA build:%s email:%s result:%s', build.dest, email, state)
            status = {
                "state": state,
                "target_url": "https://www.odoo.com/sign-cla",
                "description": "%s Odoo CLA signature check" % build.author,
                "context": "legal/cla"
            }
            build._log('check_cla', 'CLA %s' % state)
            build.repo_id.github('/repos/:owner/:repo/statuses/%s' % build.name, status, ignore_errors=True)
        # 0 is myself, -1 is everybody else, -2 nothing
        return -2
