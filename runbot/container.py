# -*- coding: utf-8 -*-
"""Containerize builds

The docker image used for the build is always tagged like this:
    odoo:runbot_tests
This file contains helpers to containerize builds with Docker.
When testing this file:
    the first parameter should be a directory containing Odoo.
    The second parameter is the exposed port
"""
from .docker_manager import DockerManager, docker
import configparser
import getpass
import io
import logging
import os
import re
import subprocess
import time

from odoo.tools import file_path

USERNAME = getpass.getuser()

_logger = logging.getLogger(__name__)
docker_stop_failures = {}


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

    def set_pres(self, pres):
        self.pres = pres

    def set_cmd(self, cmd):
        self.cmd = cmd

    def set_post(self, posts):
        self.posts = posts

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
    :return: tuple(success, msg) where success is a boolean and msg is the error message or None
    """

    with DockerManager(image_tag) as dm:
        last_step = None
        dm.result['success'] = False  # waiting for an image_id
        for chunk in dm.consume(dm.docker_client.api.build(path=build_dir, tag=image_tag, rm=True)):
            if 'stream' in chunk:
                stream = chunk['stream']
                if stream.startswith('Step '):
                    last_step = stream[:120] + '...'
                    if dm.log_progress:
                        _logger.info(last_step)
                if not dm.log_progress and ('running in' in stream or dm.duration > 1):
                    dm.log_progress = True
                    _logger.info('Building dockerfile %s', image_tag)
                    if last_step:
                        _logger.info(last_step)
                match = re.search(
                    r'(^Successfully built |sha256:)([0-9a-f]+)$',
                    chunk['stream'],
                )
                if match:
                    dm.result['image_id'] = match.group(2)
                    dm.result['success'] = True
                    break
        return dm.result


def docker_push(image_tag):
    return _docker_push(image_tag)


def _docker_push(image_tag):
    """Push a Docker image to the localy hosted docker registry
    :param image_tag: the image tag (or id) to push
    :return: tuple(success, msg) where success is a boolean and msg is the error message or None
    """

    with DockerManager(image_tag) as dm:
        image = dm.docker_client.images.get(image_tag)
        push_tag = f'127.0.0.1:5001/{image_tag}'
        image.tag(push_tag)
        for chunk in dm.consume(dm.docker_client.api.push(push_tag, stream=True)):
            if not dm.log_progress and 'Pushing' in chunk.get('status', ''):
                dm.log_progress = True
                _logger.info('Pushing %s', image_tag)
        return dm.result


def docker_pull(image_tag):
    return _docker_pull(image_tag)


def _docker_pull(image_tag):
    """Pull a docker image from a registry.
    :param image_tag: the full image tag, including the registry host
    e.g.: `dockerhub.runbot102.odoo.com/odoo:PureNobleTest`
    :return: DockerMager.result dict
    """
    with DockerManager(image_tag) as dm:
        for chunk in dm.consume(dm.docker_client.api.pull(image_tag, stream=True)):
            if not dm.log_progress and 'Downloading' in chunk['status']:
                dm.log_progress = True
                _logger.info('Pulling %s', image_tag)
        return dm.result


def docker_remove(image_tag):
    return _docker_remove(image_tag)


def _docker_remove(image_tag):
    docker_client = docker.from_env()
    try:
        docker_client.images.remove(image_tag, force=1)
    except docker.errors.APIError:
        message = f"Docker remove failed for {image_tag}"
        _logger.exception(message)
        return False
    return True


def docker_prune():
    return _docker_prune()


def _docker_prune():
    docker_client = docker.from_env()
    try:
        return docker_client.images.prune()
    except docker.errors.APIError:
        _logger.exception('Docker prune failed')
        return {'ImagesDeleted': None, 'SpaceReclaimed': 0}

def docker_run(*args, **kwargs):
    return _docker_run(*args, **kwargs)


def _docker_run(cmd=False, log_path=False, build_dir=False, container_name=False, image_tag=False, exposed_ports=None, cpu_limit=None, cpu_period=100000, cpus=0, memory=None, preexec_fn=None, ro_volumes=None, env_variables=None):
    """Run tests in a docker container
    :param run_cmd: command string to run in container
    :param log_path: path to the logfile that will contain odoo stdout and stderr
    :param build_dir: the build directory that contains the Odoo sources to build.
                      This directory is shared as a volume with the container
    :param container_name: used to give a name to the container for later reference
    :param image_tag: Docker image tag name to select which docker image to use
    :param exposed_ports: if not None, starting at 8069, ports will be exposed as exposed_ports numbers
    :param cpu_period: Specify the CPU CFS scheduler period, which is used alongside cpu_quota
    :param cpus: used to compute cpu_quota = cpu_period * cpus (equivalent of --cpus in docker CLI)
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
    run_cmd = 'cd /data/build;touch start-%s;%s;cd /data/build;touch end-%s' % (container_name, run_cmd, container_name)
    docker_clear_state(container_name, build_dir)  # ensure that no state are remaining
    build_dir = file_path(build_dir)

    file_path(os.path.dirname(log_path))
    open(os.path.join(build_dir, 'exist-%s' % container_name), 'w+').close()
    logs = open(log_path, 'w')
    logs.write("Docker command:\n%s\n=================================================\n" % cmd_object)
    # create start script
    volumes = {
        '/var/run/postgresql': {'bind': '/var/run/postgresql', 'mode': 'rw'},
        f'{build_dir}': {'bind': '/data/build', 'mode': 'rw'},
        f'{log_path}': {'bind': '/data/buildlogs.txt', 'mode': 'rw'}
    }

    if ro_volumes:
        for dest, source in ro_volumes.items():
            logs.write("Adding readonly volume '%s' pointing to %s \n" % (dest, source))
            volumes[source] = {'bind': dest, 'mode': 'ro'}
    logs.close()

    ports = {}
    if exposed_ports:
        for dp, hp in enumerate(exposed_ports, start=8069):
            ports[f'{dp}/tcp'] = ('127.0.0.1', hp)

    ulimits = [docker.types.Ulimit(name='core', soft=0, hard=0)]  # avoid core dump in containers
    if cpu_limit:
        ulimits.append(docker.types.Ulimit(name='cpu', soft=cpu_limit, hard=cpu_limit))

    docker_client = docker.from_env()
    container = docker_client.containers.run(
        image_tag,
        name=container_name,
        volumes=volumes,
        shm_size='128m',
        mem_limit=memory,
        ports=ports,
        ulimits=ulimits,
        cpu_period=cpu_period,
        cpu_quota=int(cpus * cpu_period ) if cpus else None,
        environment=env_variables,
        init=True,
        command=['/bin/bash', '-c',
                 f'exec &>> /data/buildlogs.txt ;{run_cmd}'],
        auto_remove=True,
        detach=True,
        user=USERNAME,
    )
    if container.status not in ('running', 'created') :
        _logger.error('Container %s started but status is not running or created:  %s', container_name, container.status)
    else:
        _logger.info('Started Docker container %s (%s)', container_name, container.short_id)
    return


def docker_stop(container_name, build_dir=None):
    return _docker_stop(container_name, build_dir)


def _docker_stop(container_name, build_dir):
    """Stops the container named container_name"""
    container_name = sanitize_container_name(container_name)
    _logger.info('Stopping container %s', container_name)
    if container_name in docker_stop_failures:
        if docker_stop_failures[container_name] + 60 * 60 < time.time():
            _logger.warning('Removing %s from docker_stop_failures', container_name)
            del docker_stop_failures[container_name]
        else:
            _logger.warning('Skipping %s, is in failure', container_name)
            return
    docker_client = docker.from_env()
    if build_dir:
        end_file = os.path.join(build_dir, 'end-%s' % container_name)
        subprocess.run(['touch', end_file])
    else:
        _logger.info('Stopping docker without defined build_dir')
    try:
        container = docker_client.containers.get(container_name)
        container.stop(timeout=1)
        return
    except docker.errors.NotFound:
        _logger.error('Cannnot stop container %s. Container not found', container_name)
    except docker.errors.APIError as e:
        _logger.error('Cannnot stop container %s. API Error "%s"', container_name, e)
    docker_stop_failures[container_name] = time.time()


def docker_state(container_name, build_dir):
    container_name = sanitize_container_name(container_name)
    exist = os.path.exists(os.path.join(build_dir, 'exist-%s' % container_name))
    started = os.path.exists(os.path.join(build_dir, 'start-%s' % container_name))

    if not exist:
        return 'VOID'

    if os.path.exists(os.path.join(build_dir, f'end-{container_name}')):
        return 'END'

    state = 'UNKNOWN'
    if started:
        docker_client = docker.from_env()
        try:
            container = docker_client.containers.get(container_name)
            # possible statuses: created, restarting, running, removing, paused, exited, or dead
            state = 'RUNNING' if container.status in ('created', 'running', 'paused') else 'GHOST'
        except docker.errors.NotFound:
            state = 'GHOST'
        # check if the end- file has been written in between time
        if state == 'GHOST' and os.path.exists(os.path.join(build_dir, f'end-{container_name}')):
            state = 'END'
    return state


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
    docker_client = docker.from_env()
    try:
        bridge_net = docker_client.networks.get([n.id for n in docker_client.networks.list('bridge')][0])
        return bridge_net.attrs['IPAM']['Config'][0]['Gateway']
    except (KeyError, IndexError):
        return None


def docker_ps():
    return _docker_ps()


def _docker_ps():
    """Return a list of running containers names"""
    docker_client = docker.client.from_env()
    return [c.name for c in docker_client.containers.list()]


def docker_images():
    return _docker_images()


def _docker_images():
    """Return a list of running existing images"""
    docker_client = docker.client.from_env()
    return [c for c in docker_client.images.list()]


def sanitize_container_name(name):
    """Returns a container name with unallowed characters removed"""
    name = re.sub('^[^a-zA-Z0-9]+', '', name)
    return re.sub('[^a-zA-Z0-9_.-]', '', name)
