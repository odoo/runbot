#!/usr/bin/python3
import logging
import threading

from datetime import datetime
from pathlib import Path

from tools import RunbotClient, run, docker_monitoring_loop

_logger = logging.getLogger(__name__)


class BuilderClient(RunbotClient):

    def on_start(self):
        self.last_update = datetime(1970, 1, 1)
        self.last_docker_updates = None
        if self.host.is_builder:
            builds_path = self.env['runbot.runbot']._path('build')
            monitoring_thread = threading.Thread(target=docker_monitoring_loop, args=(builds_path,), daemon=True)
            monitoring_thread.start()

    def loop_turn(self):
        if self.host.is_registry:
            self.env['runbot.runbot']._reload_nginx()
            self.env['runbot.runbot']._start_docker_registry()
        if self.host.is_registry or self.host.is_builder:
            last_docker_updates = self.env['runbot.dockerfile'].search([('to_build', '=', True)]).mapped('write_date')
            if self.count == 1 or self.last_docker_updates != last_docker_updates:
                self.last_docker_updates = last_docker_updates
                self.host._docker_update_images()
                self.env.cr.commit()
        if self.host.is_builder:
            self.last_update = self.env['runbot.repo'].search([('write_date', '>', self.last_update)])._update_git_config()
            self.env.cr.commit()
            if self.count == 1:  # cleanup at second iteration
                self.env['runbot.runbot']._source_cleanup()
                self.env.cr.commit()
                self.env['runbot.build']._local_cleanup()
                self.env.cr.commit()
                self.env['runbot.runbot']._docker_cleanup()
                self.env.cr.commit()
                self.host._set_psql_conn_count()
                self.env.cr.commit()
                self.git_gc()
                self.env.cr.commit()
            return self.env['runbot.runbot']._scheduler_loop_turn(self.host)
        else:
            self.host.last_success = datetime.now()
            return 10


if __name__ == '__main__':
    run(BuilderClient)
