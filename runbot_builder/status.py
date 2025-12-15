#!/usr/bin/python3
from tools import RunbotClient, run


class StatusClient(RunbotClient):

    def loop_turn(self):
        self.env['runbot.commit.status']._send_to_process()
        self.env.cr.commit()
        return 5


if __name__ == '__main__':
    run(StatusClient)
