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
import configparser
import datetime
import io
import json
import logging
from .common import os
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
    def __init__(self, pres, cmd, posts, finals=None, config_tuples=None):
        """ Command object that represent commands to run in Docker container
        :param pres: list of pre-commands
        :param cmd: list of main command only run if the pres commands succeed (&&)
        :param posts: list of post commands posts only run if the cmd command succedd (&&)
        :param finals: list of finals commands always executed
        :param config_tuples: list of key,value tuples to write in config file
        returns a string of the full command line to run
        """
        self.pres = pres or []
        self.cmd = cmd
        self.posts = posts or []
        self.finals = finals or []
        self.config_tuples = config_tuples or []

    def __getattr__(self, name):
        return getattr(self.cmd, name)

    def __getitem__(self, key):
        return self.cmd[key]

    def __add__(self, l):
        return Command(self.pres, self.cmd + l, self.posts, self.finals, self.config_tuples)

    def __str__(self):
        return ' '.join(self)

    def __repr__(self):
        return self.build().replace('&& ', '&&\n').replace('|| ', '||\n\t').replace(';', ';\n')

    def build(self):
        cmd_chain = []
        cmd_chain += [' '.join(pre) for pre in self.pres if pre]
        cmd_chain.append(' '.join(self))
        cmd_chain += [' '.join(post) for post in self.posts if post]
        cmd_chain = [' && '.join(cmd_chain)]
        cmd_chain += [' '.join(final) for final in self.finals if final]
        return ' ; '.join(cmd_chain)

    def add_config_tuple(self, option, value):
        assert '-' not in option
        self.config_tuples.append((option, value))

    def get_config(self, starting_config=''):
        """ returns a config file content based on config tuples and
            and eventually update the starting config
        """
        config = configparser.ConfigParser()
        config.read_string(starting_config)
        if self.config_tuples and not config.has_section('options'):
            config.add_section('options')
        for option, value in self.config_tuples:
            config.set('options', option, value)
        res = io.StringIO()
        config.write(res)
        res.seek(0)
        return res.read()


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
    :params env_variables: list of environment variables
    """
    if isinstance(run_cmd, Command):
        cmd_object = run_cmd
        run_cmd = cmd_object.build()
    else:
        cmd_object = Command([], run_cmd.split(' '), [])
    _logger.debug('Docker run command: %s', run_cmd)
    logs = open(log_path, 'w')
    run_cmd = 'cd /data/build;touch start-%s;%s;cd /data/build;touch end-%s' % (container_name, run_cmd, container_name)
    docker_clear_state(container_name, build_dir)  # ensure that no state are remaining
    logs.write("Docker command:\n%s\n=================================================\n" % cmd_object)
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
    rc_content = cmd_object.get_config(starting_config=open(final_rc, 'r').read() if final_rc else '')
    rc_path = os.path.join(build_dir, '.odoorc')
    with open(rc_path, 'w') as rc_file:
        rc_file.write(rc_content)
    docker_command.extend(['--volume=%s:/home/odoo/.odoorc:ro' % rc_path])

    if exposed_ports:
        for dp, hp in enumerate(exposed_ports, start=8069):
            docker_command.extend(['-p', '127.0.0.1:%s:%s' % (hp, dp)])
    if cpu_limit:
        docker_command.extend(['--ulimit', 'cpu=%s' % int(cpu_limit)])
    docker_command.extend(['odoo:runbot_tests', '/bin/bash', '-c', "%s" % run_cmd])
    docker_run = subprocess.Popen(docker_command, stdout=logs, stderr=logs, preexec_fn=preexec_fn, close_fds=False, cwd=build_dir)
    _logger.info('Started Docker container %s', container_name)
    return

def docker_stop(container_name, build_dir=None):
    """Stops the container named container_name"""
    _logger.info('Stopping container %s', container_name)
    if build_dir:
        end_file = os.path.join(build_dir, 'end-%s' % container_name)
        subprocess.run(['touch', end_file])
    else:
        _logger.info('Stopping docker without defined build_dir')
    subprocess.run(['docker', 'stop', container_name])

def docker_is_running(container_name):
    dinspect = subprocess.run(['docker', 'container', 'inspect', container_name], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    return True if dinspect.returncode == 0 else False

def docker_state(container_name, build_dir):
    started = os.path.exists(os.path.join(build_dir, 'start-%s' % container_name))
    ended = os.path.exists(os.path.join(build_dir, 'end-%s' % container_name))
    return 'END' if ended else 'RUNNING' if started else 'UNKNOWN'

def docker_clear_state(container_name, build_dir):
    """Return True if container is still running"""
    if os.path.exists(os.path.join(build_dir, 'start-%s' % container_name)):
        os.remove(os.path.join(build_dir, 'start-%s' % container_name))
    if os.path.exists(os.path.join(build_dir, 'end-%s' % container_name)):
        os.remove(os.path.join(build_dir, 'end-%s' % container_name))

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
    _logger.info('Logfile is in %s', logfile)
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

    with open(os.path.join(args.build_dir, 'odoo-bin'), 'r') as exfile:
        py_version = '3' if 'python3' in exfile.readline() else ''

    # Test environment variables
    if args.env:
        cmd = Command(None, ['echo testa is $TESTA and testb is $TESTB '], None)
        env_variables = ['TESTA=test a', 'TESTB="test b"']
        env_log = os.path.join(args.build_dir, 'logs', 'logs-env.txt')
        container_name = 'odoo-container-test-%s' % datetime.datetime.now().microsecond
        docker_run(cmd.build(), env_log, args.build_dir, container_name, env_variables=env_variables)
        expected = 'testa is test a and testb is "test b"'
        time.sleep(3) # ugly sleep to wait for docker process to flush the log file
        assert expected in open(env_log,'r').read()

    # Test testing
    pres = [['sudo', 'pip%s' % py_version, 'install', '-r', '/data/build/requirements.txt']]
    posts = None
    python_params = []
    if args.coverage:
        omit = ['--omit', '*__manifest__.py']
        python_params = [ '-m', 'coverage', 'run', '--branch', '--source', '/data/build'] + omit
        posts = [['python%s' % py_version, "-m", "coverage", "html", "-d", "/data/build/coverage", "--ignore-errors"]]
        os.makedirs(os.path.join(args.build_dir, 'coverage'), exist_ok=True)
    elif args.flamegraph:
        flame_log = '/data/build/logs/flame.log'
        python_params = ['-m', 'flamegraph', '-o', flame_log]
    odoo_cmd = ['python%s' % py_version ] + python_params + ['/data/build/odoo-bin', '-d %s' % args.db_name, '--addons-path=/data/build/addons', '-i', args.odoo_modules,  '--test-enable', '--stop-after-init', '--max-cron-threads=0']
    cmd = Command(pres, odoo_cmd, posts)
    cmd.add_config_tuple('data_dir', '/data/build/datadir')
    cmd.add_config_tuple('db_user', '%s' % os.getlogin())

    if args.dump:
        os.makedirs(os.path.join(args.build_dir, 'logs', args.db_name), exist_ok=True)
        dump_dir = '/data/build/logs/%s/' % args.db_name
        sql_dest = '%s/dump.sql' % dump_dir
        filestore_path = '/data/build/datadir/filestore/%s' % args.db_name
        filestore_dest = '%s/filestore/' % dump_dir
        zip_path = '/data/build/logs/%s.zip' % args.db_name
        cmd.finals.append(['pg_dump', args.db_name, '>', sql_dest])
        cmd.finals.append(['cp', '-r', filestore_path, filestore_dest])
        cmd.finals.append(['cd', dump_dir, '&&', 'zip', '-rm9', zip_path, '*'])

    if args.flamegraph:
        cmd.finals.append(['flamegraph.pl', '--title', 'Flamegraph', flame_log, '>', '/data/build/logs/flame.svg'])
        cmd.finals.append(['gzip', '-f', flame_log])

    if args.kill:
        logfile = os.path.join(args.build_dir, 'logs', 'logs-partial.txt')
        container_name = 'odoo-container-test-%s' % datetime.datetime.now().microsecond
        docker_run(cmd.build(), logfile, args.build_dir, container_name)
        # Test stopping the container
        _logger.info('Waiting 30 sec before killing the build')
        time.sleep(30)
        docker_stop(container_name)
        time.sleep(3)

    # Test full testing
    logfile = os.path.join(args.build_dir, 'logs', 'logs-full-test.txt')
    container_name = 'odoo-container-test-%s' % datetime.datetime.now().microsecond
    docker_run(cmd, logfile, args.build_dir, container_name)
    time.sleep(1)  # give time for the container to start

    while docker_is_running(container_name):
        time.sleep(10)
        _logger.info("Waiting for %s to stop", container_name)

    if args.run:
        # Test running
        logfile = os.path.join(args.build_dir, 'logs', 'logs-running.txt')
        odoo_cmd = [
            'python%s' % py_version,
            '/data/build/odoo-bin', '-d %s' % args.db_name,
            '--db-filter', '%s.*$' % args.db_name, '--addons-path=/data/build/addons',
            '-r %s' % os.getlogin(), '-i', 'web',  '--max-cron-threads=1',
            '--data-dir', '/data/build/datadir', '--workers', '2',
            '--longpolling-port', '8070']
        smtp_host = docker_get_gateway_ip()
        if smtp_host:
            odoo_cmd.extend(['--smtp', smtp_host])
        container_name = 'odoo-container-test-%s' % datetime.datetime.now().microsecond
        cmd = Command(pres, odoo_cmd, [])
        docker_run(cmd.build(), logfile, args.build_dir, container_name, exposed_ports=[args.odoo_port, args.odoo_port + 1], cpu_limit=300)


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    parser = argparse.ArgumentParser()
    subparser = parser.add_subparsers(dest='command', required='True', help='commands')
    p_build = subparser.add_parser('build', help='Build docker image')
    p_build.add_argument('build_dir')
    p_build.set_defaults(func=build)
    p_test = subparser.add_parser('tests', help='Test docker functions')
    p_test.set_defaults(func=tests)
    p_test.add_argument('build_dir')
    p_test.add_argument('odoo_port', type=int)
    p_test.add_argument('db_name')
    group = p_test.add_mutually_exclusive_group()
    group.add_argument('--coverage', action='store_true', help= 'test a build with coverage')
    group.add_argument('--flamegraph', action='store_true', help= 'test a build and draw a flamegraph')
    p_test.add_argument('-i', dest='odoo_modules', default='web', help='Comma separated list of modules')
    p_test.add_argument('--kill', action='store_true', default=False, help='Also test container kill')
    p_test.add_argument('--dump', action='store_true', default=False, help='Test database export with pg_dump')
    p_test.add_argument('--run', action='store_true', default=False, help='Also test running (Warning: the container survives exit)')
    p_test.add_argument('--env', action='store_true', default=False, help='Test passing environment variables')
    args = parser.parse_args()
    args.func(args)
