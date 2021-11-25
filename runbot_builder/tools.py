#!/usr/bin/python3
import argparse
import logging
import os
import sys
import threading
import signal

from logging.handlers import WatchedFileHandler

LOG_FORMAT = '%(asctime)s %(levelname)s %(name)s: %(message)s'
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logging.getLogger('odoo.addons.runbot').setLevel(logging.DEBUG)
logging.addLevelName(25, "!NFO")

_logger = logging.getLogger(__name__)


class RunbotClient():

    def __init__(self, env):
        self.env = env
        self.ask_interrupt = threading.Event()
        self.host = None
        self.count = 0
        self.max_count = 60

    def on_start(self):
        pass

    def main_loop(self):
        from odoo import fields
        self.on_start()
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGQUIT, self.dump_stack)
        self.host = self.env['runbot.host']._get_current()
        self.host._bootstrap()
        logging.info(
            'Host %s running with %s slots on pid %s%s',
            self.host.name,
            self.host.nb_worker,
            os.getpid(),
            ' (assigned only)' if self.host.assigned_only else ''
        )
        while True:
            try:
                self.host.last_start_loop = fields.Datetime.now()
                self.count = self.count % self.max_count
                sleep_time = self.loop_turn()
                self.count += 1
                self.host.last_end_loop = fields.Datetime.now()
                self.env.cr.commit()
                self.env.clear()
                self.sleep(sleep_time)
            except Exception as e:
                _logger.exception('Builder main loop failed with: %s', e)
                self.env.cr.rollback()
                self.env.clear()
                self.sleep(10)
            if self.ask_interrupt.is_set():
                return

    def loop_turn(self):
        raise NotImplementedError()

    def signal_handler(self, _signal, _frame):
        if self.ask_interrupt.is_set():
            _logger.info("Second Interrupt detected, force exit")
            os._exit(1)

        _logger.info("Interrupt detected")
        self.ask_interrupt.set()

    def dump_stack(self, _signal, _frame):
        import odoo
        odoo.tools.misc.dumpstacks()

    def sleep(self, t):
        self.ask_interrupt.wait(t)


def run(client_class):
    # parse args
    parser = argparse.ArgumentParser()
    parser.add_argument('--odoo-path', help='Odoo sources path')
    parser.add_argument('--db_host')
    parser.add_argument('--db_port')
    parser.add_argument('--db_user')
    parser.add_argument('--db_password')
    parser.add_argument('-d', '--database', default='runbot', help='name of runbot db')
    parser.add_argument('--logfile', default=False)
    args = parser.parse_args()
    if args.logfile:
        dirname = os.path.dirname(args.logfile)
        if dirname and not os.path.isdir(dirname):
            os.makedirs(dirname)

        handler = WatchedFileHandler(args.logfile)
        formatter = logging.Formatter(LOG_FORMAT)
        handler.setFormatter(formatter)
        logging.getLogger().addHandler(handler)

    # configure odoo
    sys.path.append(args.odoo_path)
    import odoo
    _logger.info("Starting scheduler on database %s", args.database)
    odoo.tools.config['db_host'] = args.db_host
    odoo.tools.config['db_port'] = args.db_port
    odoo.tools.config['db_user'] = args.db_user
    odoo.tools.config['db_password'] = args.db_password
    addon_path = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)), '..'))
    config_addons_path = odoo.tools.config['addons_path']
    odoo.tools.config['addons_path'] = ','.join([config_addons_path, addon_path])

    # create environment
    registry = odoo.registry(args.database)
    with odoo.api.Environment.manage():
        with registry.cursor() as cr:
            env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
            client = client_class(env)
            # run main loop
            client.main_loop()
    _logger.info("Stopping gracefully")
