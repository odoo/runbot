IMP: allow setting forward-port limits after the source pull request has been merged

Should now be possible to both extend and retract the forward port limit
afterwards, though obviously no shorter than the current tip of the forward
port sequence. One limitation is that forward ports being created can't be
stopped so there might be some windows where trying to set the limit to the
current tip will fail (because it's in the process of being forward-ported to
the next branch).
