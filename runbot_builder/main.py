#!/usr/bin/python3
from tools import run
from builder import BuilderClient
from leader import LeaderClient


# NOTE: this is an unsued client aiming to replace all clients in the future but not functionnal yet
# still need some fixing and some testing

class MainClient(LeaderClient, BuilderClient):
    def on_start(self):
        if self.host.is_leader:
            LeaderClient.on_start(self)
        if self.host.is_builder or self.host.is_registry:
            BuilderClient.on_start(self)

    def loop_turn(self):
        sleeps = [10]
        if self.host.is_leader:
            sleeps.append(LeaderClient.loop_turn(self))
        else:
            if self.host.is_assigner:
                self.env['runbot.runbot']._assign_pending_builds()
                sleeps.append(5)
        if self.host.is_builder or self.host.is_registry:
            sleeps.append(BuilderClient.loop_turn(self))
        return min(sleeps)


if __name__ == '__main__':
    run(MainClient)
