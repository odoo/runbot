REV: don't automatically close PRs when their branch is disabled

Turns out that breaks FW chains and makes existing forward ports harder to
manage, so revert that bit. Do keep sending a message on the PR tho.
