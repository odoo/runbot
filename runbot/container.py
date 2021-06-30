# -*- coding: utf-8 -*-
"""Containerize builds

The docker image used for the build is always tagged like this:
    odoo:runbot_tests
This file contains helpers to containerize builds with Docker.
When testing this file:
    the first parameter should be a directory containing Odoo.
    The second parameter is the exposed port
"""
import configparser
import io
import json
import logging
import os
import re
import subprocess


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

    def __init__(self, pres, cmd, posts, finals=None, config_tuples=None, cmd_checker=None):
        """ Command object that represent commands to run in Docker container
        :param pres: list of pre-commands
        :param cmd: list of main command only run if the pres commands succeed (&&)
        :param posts: list of post commands posts only run if the cmd command succedd (&&)
        :param finals: list of finals commands always executed
        :param config_tuples: list of key,value tuples to write in config file
        :param cmd_checker: a checker object that must have a `_cmd_check` method that will be called at build
        returns a string of the full command line to run
        """
        self.pres = pres or []
        self.cmd = cmd
        self.posts = posts or []
        self.finals = finals or []
        self.config_tuples = config_tuples or []
        self.cmd_checker = cmd_checker

    def __getattr__(self, name):
        return getattr(self.cmd, name)

    def __getitem__(self, key):
        return self.cmd[key]

    def __add__(self, l):
        return Command(self.pres, self.cmd + l, self.posts, self.finals, self.config_tuples, self.cmd_checker)

    def __str__(self):
        return ' '.join(self)

    def __repr__(self):
        return self.build().replace('&& ', '&&\n').replace('|| ', '||\n\t').replace(';', ';\n')

    def build(self):
        if self.cmd_checker:
            self.cmd_checker._cmd_check(self)
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


def docker_build(build_dir, image_tag):
    return _docker_build(build_dir, image_tag)


def _docker_build(build_dir, image_tag):
    """Build the docker image
    :param build_dir: the build directory that contains Dockerfile.
    :param image_tag: name used to tag the resulting docker image
    """
    # synchronise the current user with the odoo user inside the Dockerfile
    with open(os.path.join(build_dir, 'Dockerfile'), 'a') as df:
        df.write(DOCKERUSER)
    log_path = os.path.join(build_dir, 'docker_build.txt')
    logs = open(log_path, 'w')
    dbuild = subprocess.Popen(['docker', 'build', '--tag', image_tag, '.'], stdout=logs, stderr=logs, cwd=build_dir)
    return dbuild.wait()


def docker_run(*args, **kwargs):
    return _docker_run(*args, **kwargs)


def _docker_run(cmd=False, log_path=False, build_dir=False, container_name=False, image_tag=False, exposed_ports=None, cpu_limit=None, memory=None, preexec_fn=None, ro_volumes=None, env_variables=None):
    """Run tests in a docker container
    :param run_cmd: command string to run in container
    :param log_path: path to the logfile that will contain odoo stdout and stderr
    :param build_dir: the build directory that contains the Odoo sources to build.
                      This directory is shared as a volume with the container
    :param container_name: used to give a name to the container for later reference
    :param image_tag: Docker image tag name to select which docker image to use
    :param exposed_ports: if not None, starting at 8069, ports will be exposed as exposed_ports numbers
    :param memory: memory limit in bytes for the container
    :params ro_volumes: dict of dest:source volumes to mount readonly in builddir
    :params env_variables: list of environment variables
    """
    assert cmd and log_path and build_dir and container_name
    run_cmd = cmd
    image_tag = image_tag or 'odoo:DockerDefault'
    container_name = sanitize_container_name(container_name)
    if isinstance(run_cmd, Command):
        cmd_object = run_cmd
        run_cmd = cmd_object.build()
    else:
        cmd_object = Command([], run_cmd.split(' '), [])
    _logger.info('Docker run command: %s', run_cmd)
    logs = open(log_path, 'w')
    run_cmd = 'cd /data/build;touch start-%s;%s;cd /data/build;touch end-%s' % (container_name, run_cmd, container_name)
    docker_clear_state(container_name, build_dir)  # ensure that no state are remaining
    open(os.path.join(build_dir, 'exist-%s' % container_name), 'w+').close()
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

    if memory:
        docker_command.append('--memory=%s' % memory)

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
    docker_command.extend([image_tag, '/bin/bash', '-c', "%s" % run_cmd])
    subprocess.Popen(docker_command, stdout=logs, stderr=logs, preexec_fn=preexec_fn, close_fds=False, cwd=build_dir)
    _logger.info('Started Docker container %s', container_name)
    return


def docker_stop(container_name, build_dir=None):
    return _docker_stop(container_name, build_dir)


def _docker_stop(container_name, build_dir):
    """Stops the container named container_name"""
    container_name = sanitize_container_name(container_name)
    _logger.info('Stopping container %s', container_name)
    if build_dir:
        end_file = os.path.join(build_dir, 'end-%s' % container_name)
        subprocess.run(['touch', end_file])
    else:
        _logger.info('Stopping docker without defined build_dir')
    subprocess.run(['docker', 'stop', container_name])


def docker_is_running(container_name):
    container_name = sanitize_container_name(container_name)
    dinspect = subprocess.run(['docker', 'container', 'inspect', container_name], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    return True if dinspect.returncode == 0 else False


def docker_state(container_name, build_dir):
    container_name = sanitize_container_name(container_name)
    exist = os.path.exists(os.path.join(build_dir, 'exist-%s' % container_name))
    started = os.path.exists(os.path.join(build_dir, 'start-%s' % container_name))
    ended = os.path.exists(os.path.join(build_dir, 'end-%s' % container_name))

    if not exist:
        return 'VOID'

    if ended:
        return 'END'

    if started:
        if docker_is_running(container_name):
            return 'RUNNING'
        else:
            return 'GHOST'

    return 'UNKNOWN'


def docker_clear_state(container_name, build_dir):
    """Return True if container is still running"""
    container_name = sanitize_container_name(container_name)
    if os.path.exists(os.path.join(build_dir, 'start-%s' % container_name)):
        os.remove(os.path.join(build_dir, 'start-%s' % container_name))
    if os.path.exists(os.path.join(build_dir, 'end-%s' % container_name)):
        os.remove(os.path.join(build_dir, 'end-%s' % container_name))
    if os.path.exists(os.path.join(build_dir, 'exist-%s' % container_name)):
        os.remove(os.path.join(build_dir, 'exist-%s' % container_name))


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
    return _docker_ps()


def _docker_ps():
    """Return a list of running containers names"""
    try:
        docker_ps = subprocess.run(['docker', 'ps', '--format', '{{.Names}}'], stderr=subprocess.DEVNULL, stdout=subprocess.PIPE)
    except FileNotFoundError:
        _logger.warning('Docker not found, returning an empty list.')
        return []
    if docker_ps.returncode != 0:
        return []
    output = docker_ps.stdout.decode()
    if not output:
        return []
    return output.strip().split('\n')


def build(args):
    """Build container from CLI"""
    _logger.info('Building the base image container')
    logdir = os.path.join(args.build_dir, 'logs')
    os.makedirs(logdir, exist_ok=True)
    logfile = os.path.join(logdir, 'logs-build.txt')
    _logger.info('Logfile is in %s', logfile)
    docker_build(logfile, args.build_dir)
    _logger.info('Finished building the base image container')


def sanitize_container_name(name):
    """Returns a container name with unallowed characters removed"""
    name = re.sub('^[^a-zA-Z0-9]+', '', name)
    return re.sub('[^a-zA-Z0-9_.-]', '', name)



##############################################################################
# Ugly monkey patch to set runbot in set runbot in testing mode
# No Docker will be started, instead a fake docker_run function will be used
##############################################################################

if os.environ.get('RUNBOT_MODE') == 'test':
    _logger.warning('Using Fake Docker')

    def fake_docker_run(run_cmd, log_path, build_dir, container_name, exposed_ports=None, cpu_limit=None, preexec_fn=None, ro_volumes=None, env_variables=None, *args, **kwargs):
        _logger.info('Docker Fake Run: %s', run_cmd)
        open(os.path.join(build_dir, 'exist-%s' % container_name), 'w').write('fake end')
        open(os.path.join(build_dir, 'start-%s' % container_name), 'w').write('fake start\n')
        open(os.path.join(build_dir, 'end-%s' % container_name), 'w').write('fake end')
        with open(log_path, 'w') as log_file:
            log_file.write('Fake docker_run started\n')
            log_file.write('run_cmd: %s\n' % run_cmd)
            log_file.write('build_dir: %s\n' % container_name)
            log_file.write('container_name: %s\n' % container_name)
            log_file.write('.modules.loading: Modules loaded.\n')
            log_file.write('Initiating shutdown\n')

    docker_run = fake_docker_run
