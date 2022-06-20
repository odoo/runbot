#!/usr/bin/python3
from tools import RunbotClient, run
import logging
import time

_logger = logging.getLogger(__name__)

class LeaderClient(RunbotClient):  # Conductor, Director, Main, Maestro, Lead
    def __init__(self, env):
        self.pull_info_failures = {}
        super().__init__(env)

    def on_start(self):
        _logger.info('Updating all repos')
        for repo in self.env['runbot.repo'].search([('mode', '!=', 'disabled')]):
            repo._update(force=True)
        _logger.info('update finished')

    def loop_turn(self):
        if self.count == 0:
            self.env['runbot.repo']._update_git_config()
            self.git_gc()
        return self.env['runbot.runbot']._fetch_loop_turn(self.host, self.pull_info_failures)


if __name__ == '__main__':
    run(LeaderClient)
