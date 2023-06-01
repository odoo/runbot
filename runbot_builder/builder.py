#!/usr/bin/python3
import logging
import threading

from pathlib import Path

from tools import RunbotClient, run, docker_monitoring_loop

_logger = logging.getLogger(__name__)


class BuilderClient(RunbotClient):

    def on_start(self):
        builds_path = Path(self.env['runbot.runbot']._root()) / 'build'
        monitoring_thread = threading.Thread(target=docker_monitoring_loop, args=(builds_path,), daemon=True)
        monitoring_thread.start()

        for repo in self.env['runbot.repo'].search([('mode', '!=', 'disabled')]):
            repo._update(force=True)

    def loop_turn(self):
        if self.count == 1: # cleanup at second iteration
            self.env['runbot.runbot']._source_cleanup()
            self.env['runbot.build']._local_cleanup()
            self.env['runbot.runbot']._docker_cleanup()
            self.host.set_psql_conn_count()
            self.host._docker_build()
            self.env['runbot.repo']._update_git_config()
            self.git_gc()
        return self.env['runbot.runbot']._scheduler_loop_turn(self.host)


if __name__ == '__main__':
    run(BuilderClient)
