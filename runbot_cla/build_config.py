# -*- encoding: utf-8 -*-

import glob
import io
import logging
import re

from odoo import models, fields

_logger = logging.getLogger(__name__)


class Step(models.Model):
    _inherit = "runbot.build.config.step"

    job_type = fields.Selection(selection_add=[('cla_check', 'Check cla')])

    def _run_step(self, build, log_path):
        if self.job_type == 'cla_check':
            return self._runbot_cla_check(build, log_path)
        return super(Step, self)._run_step(build, log_path)

    def _runbot_cla_check(self, build, log_path):
        build._checkout()
        cla_glob = glob.glob(build._get_server_commit()._source_path("doc/cla/*/*.md"))
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
                        cla = ''.join(io.open(f, encoding='utf-8').read() for f in cla_glob)
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
            build._github_status_notify_all(status)
        # 0 is myself, -1 is everybody else, -2 nothing
        return -2
