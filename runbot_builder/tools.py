#!/usr/bin/python3
import argparse
import docker
import logging
import os
import psutil
import re
import sys
import threading
import time
import signal

from datetime import datetime, timedelta, timezone
from pathlib import Path
from logging.handlers import WatchedFileHandler

LOG_FORMAT = '%(asctime)s %(levelname)s %(name)s: %(message)s'
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logging.getLogger('odoo.addons.runbot').setLevel(logging.DEBUG)
logging.addLevelName(25, "!NFO")

CPU_COUNT = os.cpu_count()
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
        self.update_next_git_gc_date()
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
                self.env.cr.commit()
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

    def update_next_git_gc_date(self):
        now = datetime.now()
        gc_hour = int(self.env['ir.config_parameter'].sudo().get_param('runbot.git_gc_hour', '23'))
        gc_minutes = self.host.id % 60  # deterministic minutes
        self.next_git_gc_date = datetime(now.year, now.month, now.day, gc_hour, gc_minutes)
        while self.next_git_gc_date <= now:
            self.next_git_gc_date += timedelta(days=1)
        _logger.info('Next git gc scheduled on %s', self.next_git_gc_date)

    def git_gc(self):
        """ git gc once a day """
        if self.next_git_gc_date < datetime.now():
            _logger.info('Starting git gc on repositories')
            self.env['runbot.runbot']._git_gc(self.host)
            self.update_next_git_gc_date()

def run(client_class):
    # parse args
    parser = argparse.ArgumentParser()
    parser.add_argument('--odoo-path', help='Odoo sources path')
    parser.add_argument('--db_host')
    parser.add_argument('--db_port')
    parser.add_argument('--db_user')
    parser.add_argument('--db_password')
    parser.add_argument('-d', '--database', default='runbot', help='name of runbot db')
    parser.add_argument('--addons-path', type=str, dest="addons_path")
    parser.add_argument('--logfile', default=False)
    parser.add_argument('--forced-host-name', default=False)

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
    config_addons_path = args.addons_path or odoo.tools.config['addons_path']
    if addon_path not in config_addons_path:
        config_addons_path = ','.join([config_addons_path, addon_path])
    odoo.tools.config['addons_path'] = config_addons_path
    odoo.tools.config['forced_host_name'] = args.forced_host_name

    # create environment
    registry = odoo.registry(args.database)
    with odoo.api.Environment.manage():
        with registry.cursor() as cr:
            env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
            client = client_class(env)
            # run main loop
            try:
                client.main_loop()
            except Exception as e:
                _logger.exception(str(e))
                raise e
    _logger.info("Stopping gracefully")

def human_size(nb):
    for i, u in enumerate(['', ' KiB', ' MiB', ' GiB']):
        if nb < 1000 or i == 3:  # 1000 to avoid things like 1001 MiB
            break
        nb = nb / 1024
    return f'{nb:.2f}{u}'

def get_cpu_time():
    """returns system cpus times in microseconds"""
    return sum([t for t in psutil.cpu_times()]) * 10 ** 6

def get_docker_stats(container_id):
    """
    Cgroup V1 path is /sys/fs/cgroup/memory/docker/{container_id}/memory.usage_in_bytes'
    https://docs.docker.com/config/containers/runmetrics/#find-the-cgroup-for-a-given-container
    on our runbot, we use cgroup V2 + systemd driver
    We could add a method to find the right path instead of hard coding
    Returns a tupple with:
    (time, current memory usage, cpu usage in microseconds, current system usage microseconds)
    """
    try:
        memory_current = int(Path(f'/sys/fs/cgroup/system.slice/docker-{container_id}.scope/memory.current').read_text().strip())
    except Exception:  # should we log exception in order to debug ... but will spam the logs ...
        memory_current = 0
    try:
        cpu_stats = Path(f'/sys/fs/cgroup/system.slice/docker-{container_id}.scope/cpu.stat').read_text()
        cpu_usage_line = cpu_stats.split('\n')[0]
        usage_usec = cpu_usage_line and int(cpu_usage_line.split()[1])
    except Exception:
        usage_usec = 0
    return (time.time(), memory_current, usage_usec, get_cpu_time())

def prepare_stats_log(dest, previous_stats, current_stats):
    current_time, current_mem, current_cpu, current_cpu_time = current_stats

    if not previous_stats:
        return (current_time, 0, 0, current_cpu, current_cpu_time), ''

    _, logged_mem, logged_cpu_percent, previous_cpu, previous_cpu_time,  = previous_stats

    mem_ratio = (current_mem - logged_mem) / (logged_mem or 1) * 100
    cpu_delta = current_cpu - previous_cpu
    system_delta = (current_cpu_time - previous_cpu_time) / CPU_COUNT
    cpu_percent = cpu_delta / system_delta * 100
    cpu_percent_delta = (cpu_percent - logged_cpu_percent)
    cpu_percent_ratio =  cpu_percent_delta / (logged_cpu_percent or 0.000001) * 100

    log_lines = []
    date_time = datetime.fromtimestamp(current_time).astimezone(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
    if abs(mem_ratio) > 10:  #  hard-coded values. We should find a way to configure.
        humanized = f'({human_size(current_mem)})'
        log_lines.append(f'{date_time} Memory: {current_mem:>11} {humanized:>11} ({mem_ratio:>+5.1f}%)')
        logged_mem = current_mem
    if abs(cpu_percent_ratio) > 20 and abs(cpu_percent_delta) > 2:
        log_lines.append(f'{date_time}    CPU: {cpu_percent:>11.2f} ({cpu_percent_ratio:>+5.1f}%)')
        logged_cpu_percent = cpu_percent
    previous_stats = current_time, logged_mem, logged_cpu_percent, current_cpu, current_cpu_time
    return previous_stats, '\n'.join(log_lines)

def docker_monitoring_loop(builds_dir):
    docker_client = docker.from_env()
    previous_stats_per_docker = {}
    _logger.info('Starting docker monitoring loop thread')
    while True:
        try:
            stats_per_docker = dict()
            for container in docker_client.containers.list(filters={'status': 'running'}):
                if re.match(r'^\d+-.+_.+', container.name):
                    dest, suffix = container.name.split('_', maxsplit=1)
                    container_log_dir = builds_dir / dest / 'logs'
                    if not container_log_dir.exists():
                        _logger.warning('Log dir not found: `%s`', container_log_dir)
                        continue
                    current_stats = get_docker_stats(container.id)
                    previous_stats = previous_stats_per_docker.get(container.name)
                    previous_stats, log_line = prepare_stats_log(dest, previous_stats, current_stats)
                    if log_line:
                        stat_log_file = container_log_dir / f'{suffix}-stats.txt'
                        stat_log_file.open(mode='a').write(f'{log_line}\n')
                    stats_per_docker[container.name] = previous_stats
            previous_stats_per_docker = stats_per_docker
            time.sleep(1)
        except Exception as e:
            _logger.exception('Monitoring loop thread exception: %s', e)
            time.sleep(60)
