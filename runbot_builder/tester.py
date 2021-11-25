#!/usr/bin/python3
from tools import RunbotClient, run
import logging

_logger = logging.getLogger(__name__)

class TesterClient(RunbotClient):

    def loop_turn(self):
        _logger.info('='*50)
        _logger.info('Testing: %s', self.env['runbot.build'].search_count([('local_state', '=', 'testing')]))
        _logger.info('Pending: %s', self.env['runbot.build'].search_count([('local_state', '=', 'pending')]))
        return 10

if __name__ == '__main__':
    run(TesterClient)
