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

    def main_loop(self):
        from odoo import fields
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        host = self.env['runbot.host']._get_current()
        count = 0
        while True:
            try:
                host.last_start_loop = fields.Datetime.now()
                count = count % 60
                if count == 0:
                    logging.info('Host %s running with %s slots on pid %s%s', host.name, host.get_nb_worker(), os.getpid(), ' (assigned only)' if host.assigned_only else '')
                    self.env['runbot.repo']._source_cleanup()
                    self.env['runbot.build']._local_cleanup()
                    self.env['runbot.repo']._docker_cleanup()
                    host.set_psql_conn_count()
                    _logger.info('Scheduling...')
                count += 1
                sleep_time = self.env['runbot.repo']._scheduler_loop_turn(host)
                host.last_end_loop = fields.Datetime.now()
                self.env.cr.commit()
                self.env.reset()
                self.sleep(sleep_time)
            except Exception as e:
                _logger.exception('Builder main loop failed with: %s', e)
                self.env.cr.rollback()
                self.env.reset()
                self.sleep(10)

            if self.ask_interrupt.is_set():
                return

    def signal_handler(self, signal, frame):
        if self.ask_interrupt.is_set():
            _logger.info("Second Interrupt detected, force exit")
            os._exit(1)

        _logger.info("Interrupt detected")
        self.ask_interrupt.set()

    def sleep(self, t):
        self.ask_interrupt.wait(t)


def run():
    # parse args
    parser = argparse.ArgumentParser()
    parser.add_argument('--odoo-path', help='Odoo sources path')
    parser.add_argument('--db_host', default='127.0.0.1')
    parser.add_argument('--db_port', default='5432')
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
            runbot_client = RunbotClient(env)
            # run main loop
            runbot_client.main_loop()


if __name__ == '__main__':
    run()
    _logger.info("Stopping gracefully")
