#!/usr/bin/python3
from tools import RunbotClient, run
import logging

from datetime import datetime


_logger = logging.getLogger(__name__)


class LeaderClient(RunbotClient):
    def __init__(self, env):
        self.pull_info_failures = {}
        self.last_update = datetime(1970, 1, 1)
        super().__init__(env)

    def loop_turn(self):
        if not self.host.is_leader:
            _logger.warning('Leader client is not a leader host, skipping loop_turn')
            return 10

        self.last_update = self.env['runbot.repo'].search([('write_date', '>', self.last_update)])._update_git_config()
        self.env.cr.commit()
        if self.count == 0:
            self.git_gc()
            self.env.cr.commit()

        if self.host.send_status:
            self.env['runbot.commit.status']._send_to_process()
            self.env.cr.commit()

        return self.env['runbot.runbot']._fetch_loop_turn(self.host, self.pull_info_failures)


if __name__ == '__main__':
    run(LeaderClient)
