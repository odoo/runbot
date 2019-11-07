# -*- coding: utf-8 -*-
"""Containerize builds

The docker image used for the build is always tagged like this:
    odoo:runbot_tests
This file contains helpers to containerize builds with Docker.
When testing this file:
    the first parameter should be a directory containing Odoo.
    The second parameter is the exposed port
"""
import argparse
import datetime
import json
import logging
import os
import shutil
import subprocess
import time


_logger = logging.getLogger(__name__)
DOCKERUSER = """
RUN groupadd -g %(group_id)s odoo \\
&& useradd -u %(user_id)s -g odoo -G audio,video odoo \\
&& mkdir /home/odoo \\
&& chown -R odoo:odoo /home/odoo \\
&& echo "odoo ALL= NOPASSWD: /usr/bin/pip" > /etc/sudoers.d/pip \\
&& echo "odoo ALL= NOPASSWD: /usr/bin/pip3" >> /etc/sudoers.d/pip
USER odoo
ENV COVERAGE_FILE /data/build/.coverage
""" % {'group_id': os.getgid(), 'user_id': os.getuid()}


class Command():
    def __init__(self, pres, cmd, posts, finals=None):
        self.pres = pres or []
        self.cmd = cmd
        self.posts = posts or []
        self.finals = finals or []

    def __getattr__(self, name):
        return getattr(self.cmd, name)

    def __getitem__(self, key):
        return self.cmd[key]

    def __add__(self, l):
        return Command(self.pres, self.cmd + l, self.posts, self.finals)

    def build(self):
        cmd_chain = []
        cmd_chain += [' '.join(pre) for pre in self.pres if pre]
        cmd_chain.append(' '.join(self))
        cmd_chain += [' '.join(post) for post in self.posts if post]
        cmd_chain = [' && '.join(cmd_chain)]
        cmd_chain += [' '.join(final) for final in self.finals if final]
        return ' ; '.join(cmd_chain)


def docker_build(log_path, build_dir):
    """Build the docker image
    :param log_path: path to the logfile that will contain odoo stdout and stderr
    :param build_dir: the build directory that contains the Odoo sources to build.
    """
    # Prepare docker image
    docker_dir = os.path.join(build_dir, 'docker')
    os.makedirs(docker_dir, exist_ok=True)
    shutil.copy(os.path.join(os.path.dirname(__file__), 'data', 'Dockerfile'), docker_dir)
    # synchronise the current user with the odoo user inside the Dockerfile
    with open(os.path.join(docker_dir, 'Dockerfile'), 'a') as df:
        df.write(DOCKERUSER)
    logs = open(log_path, 'w')
    dbuild = subprocess.Popen(['docker', 'build', '--tag', 'odoo:runbot_tests', '.'], stdout=logs, stderr=logs, cwd=docker_dir)
    dbuild.wait()


def docker_run(run_cmd, log_path, build_dir, container_name, exposed_ports=None, cpu_limit=None, preexec_fn=None, ro_volumes=None, env_variables=None):
    """Run tests in a docker container
    :param run_cmd: command string to run in container
    :param log_path: path to the logfile that will contain odoo stdout and stderr
    :param build_dir: the build directory that contains the Odoo sources to build.
                      This directory is shared as a volume with the container
    :param container_name: used to give a name to the container for later reference
    :param exposed_ports: if not None, starting at 8069, ports will be exposed as exposed_ports numbers
    :params ro_volumes: dict of dest:source volumes to mount readonly in builddir
    """
    _logger.debug('Docker run command: %s', run_cmd)
    logs = open(log_path, 'w')
    run_cmd = 'cd /data/build && %s' % run_cmd
    logs.write("Docker command:\n%s\n=================================================\n" % run_cmd.replace('&& ', '&&\n').replace('|| ', '||\n\t'))
    # create start script
    docker_command = [
        'docker', 'run', '--rm',
        '--name', container_name,
        '--volume=/var/run/postgresql:/var/run/postgresql',
        '--volume=%s:/data/build' % build_dir,
        '--shm-size=128m',
        '--init',
    ]
    if ro_volumes:
        for dest, source in ro_volumes.items():
            logs.write("Adding readonly volume '%s' pointing to %s \n" % (dest, source))
            docker_command.append('--volume=%s:/data/build/%s:ro' % (source, dest))

    if env_variables:
        for var in env_variables:
            docker_command.append('-e=%s' % var)

    serverrc_path = os.path.expanduser('~/.openerp_serverrc')
    odoorc_path = os.path.expanduser('~/.odoorc')
    final_rc = odoorc_path if os.path.exists(odoorc_path) else serverrc_path if os.path.exists(serverrc_path) else None
    if final_rc:
        docker_command.extend(['--volume=%s:/home/odoo/.odoorc:ro' % final_rc])
    if exposed_ports:
        for dp, hp in enumerate(exposed_ports, start=8069):
            docker_command.extend(['-p', '127.0.0.1:%s:%s' % (hp, dp)])
    if cpu_limit:
        docker_command.extend(['--ulimit', 'cpu=%s' % int(cpu_limit)])
    docker_command.extend(['odoo:runbot_tests', '/bin/bash', '-c', "%s" % run_cmd])
    docker_run = subprocess.Popen(docker_command, stdout=logs, stderr=logs, preexec_fn=preexec_fn, close_fds=False, cwd=build_dir)
    _logger.info('Started Docker container %s', container_name)
    return docker_run.pid

def docker_stop(container_name):
    """Stops the container named container_name"""
    _logger.info('Stopping container %s', container_name)
    dstop = subprocess.run(['docker', 'stop', container_name])

def docker_is_running(container_name):
    """Return True if container is still running"""
    dinspect = subprocess.run(['docker', 'container', 'inspect', container_name], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    return True if dinspect.returncode == 0 else False

def docker_get_gateway_ip():
    """Return the host ip of the docker default bridge gateway"""
    docker_net_inspect = subprocess.run(['docker', 'network', 'inspect', 'bridge'], stdout=subprocess.PIPE)
    if docker_net_inspect.returncode != 0:
        return None
    if docker_net_inspect.stdout:
        try:
            return json.loads(docker_net_inspect.stdout)[0]['IPAM']['Config'][0]['Gateway']
        except KeyError:
            return None

def docker_ps():
    """Return a list of running containers names"""
    try:
        docker_ps = subprocess.run(['docker', 'ps', '--format', '{{.Names}}'], stderr=subprocess.DEVNULL, stdout=subprocess.PIPE)
    except FileNotFoundError:
        _logger.warning('Docker not found, returning an empty list.')
        return []
    if docker_ps.returncode != 0:
        return []
    return docker_ps.stdout.decode().strip().split('\n')

def build(args):
    """Build container from CLI"""
    _logger.info('Building the base image container')
    logdir = os.path.join(args.build_dir, 'logs')
    os.makedirs(logdir, exist_ok=True)
    logfile = os.path.join(logdir, 'logs-build.txt')
    docker_build(logfile, args.build_dir)
    _logger.info('Finished building the base image container')

def tests(args):
    _logger.info('Start container tests')
    os.makedirs(os.path.join(args.build_dir, 'logs'), exist_ok=True)
    os.makedirs(os.path.join(args.build_dir, 'datadir'), exist_ok=True)

    if args.kill:
        # Test stopping a non running container
        _logger.info('Test killing an non existing container')
        docker_stop('xy' * 5)
    # Test building
    _logger.info('Test building the base image container')
    logfile = os.path.join(args.build_dir, 'logs', 'logs-build.txt')
    docker_build(logfile, args.build_dir)

    # Test testing
    odoo_cmd = ['/data/build/odoo-bin', '-d %s' % args.db_name, '--addons-path=/data/build/addons', '--data-dir', '/data/build/datadir', '-r %s' % os.getlogin(), '-i', args.odoo_modules,  '--test-enable', '--stop-after-init', '--max-cron-threads=0']

    if args.kill:
        logfile = os.path.join(args.build_dir, 'logs', 'logs-partial.txt')
        container_name = 'odoo-container-test-%s' % datetime.datetime.now().microsecond
        # FIXME
        # docker_run(build_odoo_cmd(odoo_cmd), logfile, args.build_dir, container_name)
        # Test stopping the container
        _logger.info('Waiting 30 sec before killing the build')
        time.sleep(30)
        docker_stop(container_name)
        time.sleep(3)

    # Test full testing
    logfile = os.path.join(args.build_dir, 'logs', 'logs-full-test.txt')
    container_name = 'odoo-container-test-%s' % datetime.datetime.now().microsecond
    if args.coverage:
        omit = ['--omit', '*__manifest__.py']
        with open(os.path.join(args.build_dir, 'odoo-bin'), 'r') as exfile:
            pyversion = 'python3' if 'python3' in exfile.readline() else 'python'
        odoo_cmd = [ pyversion, '-m', 'coverage', 'run', '--branch', '--source', '/data/build'] + omit + odoo_cmd
    # FIXME
    # docker_run(build_odoo_cmd(odoo_cmd), logfile, args.build_dir, container_name)
    time.sleep(1)  # give time for the container to start

    while docker_is_running(container_name):
        time.sleep(10)
        _logger.info("Waiting for %s to stop", container_name)

    if args.dump:
        _logger.info("Testing pg_dump")
        logfile = os.path.join(args.build_dir, 'logs', 'logs-pg_dump.txt')
        container_name = 'odoo-container-test-pg_dump-%s' % datetime.datetime.now().microsecond
        docker_pg_dump_cmd = 'cd /data/build/datadir && pg_dump -U %s -f db_export.sql %s' % (os.getlogin(), args.db_name)
        docker_run(docker_pg_dump_cmd, logfile, args.build_dir, container_name)
        time.sleep(1)
        while docker_is_running(container_name):
            time.sleep(10)
            _logger.info("Waiting for %s to stop", container_name)

    if args.run:
        # Test running
        logfile = os.path.join(args.build_dir, 'logs', 'logs-running.txt')
        odoo_cmd = [
            '/data/build/odoo-bin', '-d %s' % args.db_name,
            '--db-filter', '%s.*$' % args.db_name, '--addons-path=/data/build/addons',
            '-r %s' % os.getlogin(), '-i', 'web',  '--max-cron-threads=1',
            '--data-dir', '/data/build/datadir', '--workers', '2',
            '--longpolling-port', '8070']
        smtp_host = docker_get_gateway_ip()
        if smtp_host:
            odoo_cmd.extend(['--smtp', smtp_host])
        container_name = 'odoo-container-test-%s' % datetime.datetime.now().microsecond

        # FIXME
        # docker_run(build_odoo_cmd(odoo_cmd), logfile, args.build_dir, container_name, exposed_ports=[args.odoo_port, args.odoo_port + 1], cpu_limit=300)

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    parser = argparse.ArgumentParser()
    subparser = parser.add_subparsers(help='commands')
    p_build = subparser.add_parser('build', help='Build docker image')
    p_build.add_argument('build_dir')
    p_build.set_defaults(func=build)
    p_test = subparser.add_parser('tests', help='Test docker functions')
    p_test.set_defaults(func=tests)
    p_test.add_argument('build_dir')
    p_test.add_argument('odoo_port', type=int)
    p_test.add_argument('db_name')
    p_test.add_argument('--coverage', action='store_true', help= 'test a build with coverage')
    p_test.add_argument('-i', dest='odoo_modules', default='web', help='Comma separated list of modules')
    p_test.add_argument('--kill', action='store_true', default=False, help='Also test container kill')
    p_test.add_argument('--dump', action='store_true', default=False, help='Test database export with pg_dump')
    p_test.add_argument('--run', action='store_true', default=False, help='Also test running (Warning: the container survives exit)')
    args = parser.parse_args()
    args.func(args)
