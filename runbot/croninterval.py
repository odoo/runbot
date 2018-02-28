# -*- coding: utf-8 -*-

import odoo
from dateutil.relativedelta import relativedelta

# increase cron frequency from 0.016 Hz to 0.1 Hz to reduce starvation and improve throughput with many workers
# TODO: find a nicer way than monkey patch to accomplish this
odoo.service.server.SLEEP_INTERVAL = 10
odoo.addons.base.ir.ir_cron._intervalTypes['minutes'] = lambda interval: relativedelta(seconds=interval * 10)
