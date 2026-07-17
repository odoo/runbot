#!/usr/bin/python3
from tools import RunbotClient, run


class AssignerClient(RunbotClient):

    def loop_turn(self):
        if self.host.is_assigner:
            with self.env['runbot.runbot']._manage_host_exception(self.host):
                self.env['runbot.runbot']._assign_pending_builds()
        self.env.cr.commit()
        return 5


if __name__ == '__main__':
    run(AssignerClient)
