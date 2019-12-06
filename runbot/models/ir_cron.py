
import threading
import logging
import odoo
from dateutil.relativedelta import relativedelta

from odoo import models, fields, api, SUPERUSER_ID

odoo.service.server.SLEEP_INTERVAL = 5
odoo.addons.base.ir.ir_cron._intervalTypes['seconds'] = lambda interval: relativedelta(seconds=interval)

_logger = logging.getLogger(__name__)


class ir_cron(models.Model):
    _inherit = "ir.cron"

    interval_type = fields.Selection(selection_add=[('seconds', 'Seconds')])

    @classmethod
    def _process_jobs(cls, db_name):
        # concurrent _cron_fetch_and_schedule and _cron_fetch_and_build may cause issue.
        # only accept to use runbot in single cron mode
        if odoo.tools.config['max_cron_threads'] == 1:
            try:
                db = odoo.sql_db.db_connect(db_name)
                threading.current_thread().dbname = db_name
                enable_default_crons = True
                with db.cursor() as cr:
                    with api.Environment.manage():
                        env = api.Environment(cr, SUPERUSER_ID, {})
                        host = env['runbot.host']._get_current()
                        enable_default_crons = host.enable_default_crons
                        if host.enable_create_build_cron:
                            env['runbot.repo']._cron_fetch_and_schedule()
                        if host.enable_run_build_cron:
                            env['runbot.repo']._cron_fetch_and_build()
            except Exception as e:
                _logger.error('An error occured while procession automated runbot cron job.\n%s', e)
            finally:
                if hasattr(threading.current_thread(), 'dbname'):
                    del threading.current_thread().dbname
        else:
            _logger.debug('Runbot automated crons can only be used in single cron worker mode, use max_cron_threads = 1')
            # another solution would be to create a lock on host, but this would complexify this code

        if enable_default_crons:
            super()._process_jobs(cls, db_name)
