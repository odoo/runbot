#!/usr/bin/python3
from tools import RunbotClient, run
import logging

_logger = logging.getLogger(__name__)

class BuilderClient(RunbotClient):

    def on_start(self):
        self.env['runbot.repo'].search([('mode', '!=', 'disabled')])._update()

    def loop_turn(self):
        if self.count == 1: # cleanup at second iteration
            self.env['runbot.runbot']._source_cleanup()
            self.env['runbot.build']._local_cleanup()
            self.env['runbot.runbot']._docker_cleanup()
            self.host.set_psql_conn_count()
            self.host._docker_build()
        return self.env['runbot.runbot']._scheduler_loop_turn(self.host)


if __name__ == '__main__':
    run(BuilderClient)
