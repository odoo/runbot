
import getpass
import logging
import time
import warnings

# unsolved issue https://github.com/docker/docker-py/issues/2928
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="The distutils package is deprecated.*",
        category=DeprecationWarning,
    )
    import docker

USERNAME = getpass.getuser()

_logger = logging.getLogger(__name__)
docker_stop_failures = {}


class DockerManager:
    def __init__(self, image_tag):
        self.image_tag = image_tag

    def __enter__(self):
        self.start = time.time()
        self.duration = 0
        self.docker_client = docker.from_env()
        self.result = {
            'msg': '',
            'image': False,
            'success': True,
        }
        self.log_progress = False
        return self

    def consume(self, stream):
        for chunk in docker.utils.json_stream.json_stream(stream):
            self.duration = time.time() - self.start
            if 'error' in chunk:
                _logger.error(chunk['error'])
                self.result['msg'] += chunk['error']
                # self.result['msg'] += str(chunk.get('errorDetail', ''))
                self.result['msg'] += '\n'
                self.result['success'] = False
                break
            if 'stream' in chunk:
                self.result['msg'] += chunk['stream']
            if 'status' in chunk:
                self.result['msg'] += chunk['status']
                if 'progress' in chunk:
                    self.result['msg'] += chunk['progress']
                self.result['msg'] += '\n'
            yield chunk

    def __exit__(self, exception_type, exception_value, exception_traceback):
        if self.log_progress:
            _logger.info('Finished in %.2fs', self.duration)
            self.result['log_progress'] = self.log_progress
        if exception_value:
            self.result['success'] = False
            _logger.warning(exception_value)
            self.result['msg'] += str(exception_value)
        self.result['duration'] = self.duration
        if self.result['success']:
            self.result['image'] = self.docker_client.images.get(self.image_tag)
            if 'image_id' in self.result and self.result['image_id'] not in self.result['image'].id:
                _logger.warning('Image id does not match %s %s', self.result['image_id'], self.result['image'].id)
                # if this never triggers, we could remove or simplify the success check from docker_build
