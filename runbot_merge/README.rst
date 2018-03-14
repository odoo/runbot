Merge Bot
=========

Setup
-----

* Setup a project with relevant repositories and branches the bot
  should manage (e.g. odoo/odoo and 10.0).
* Set up reviewers (github_login + boolean flag on partners).
* Sync PRs.
* Add "Issue comments","Pull requests" and "Statuses" webhooks to
  managed repositories.
* If applicable, add "Statuses" webhook to the *source* repositories.

  Github does not seem to send statuses cross-repository when commits
  get transmigrated so if a user creates a branch in odoo-dev/odoo,
  waits for CI to run then creates a PR targeted to odoo/odoo the PR
  will never get status-checked (unless we modify runbot to re-send
  statuses on pull_request webhook).

Working Principles
------------------

Useful information (new PRs, CI, comments, ...) is pushed to the MB
via webhooks. Most of the staging work is performed via a cron job:

1. for each active staging, check if their are done

   1. if successful

      * ``push --ff`` to target branches
      * close PRs

   2. if only one batch, mark as failed

      for batches of multiple PRs, the MB attempts to infer which
      specific PR failed

   3. otherwise split staging in 2 (bisection search of problematic
      batch)

2. for each branch with no active staging

   * if there are inactive stagings, stage one of them
   * otherwise look for batches targered to that PR (PRs grouped by
     label with branch as target)
   * attempt staging

     1. reset temp branches (one per repo) to corresponding targets
     2. merge each batch's PR into the relevant temp branch

        * on merge failure, mark PRs as failed

     3. once no more batch or limit reached, reset staging branches to
        tmp
     4. mark staging as active

Commands
--------

A command string is a line starting with the mergebot's name and
followed by various commands. Self-reviewers count as reviewers for
the purpose of their own PRs, but delegate reviewers don't.

retry
  resets a PR in error mode to ready for staging

  can be used by a reviewer or the PR author to re-stage the PR after
  it's been updated or the target has been updated & fixed.

r(review)+
  approves a PR, can be used by a reviewer or delegate reviewer

r(eview)-
  removes approval from a PR, currently only active for PRs in error
  mode: unclear what should happen if a PR got unapproved while in
  staging (cancel staging?), can be used by a reviewer or the PR
  author

squash+/squash-
  marks the PR as squash or merge, can override squash inference or a
  previous squash command, can only be used by reviewers

delegate+/delegate=<users>
  adds either PR author or the specified (github) users as authorised
  reviewers for this PR. ``<users>`` is a comma-separated list of
  github usernames (no @), can be used by reviewers

p(riority)=2|1|0
  sets the priority to normal (2), pressing (1) or urgent (0),
  lower-priority PRs are selected first and batched together, can be
  used by reviewers

  currently only used for staging, but p=0 could cancel an active
  staging to force staging the specific PR and ignore CI on the PR
  itself? AKA pr=0 would cancel a pending staging and ignore
  (non-error) state? Q: what of co-dependent PRs, staging currently
  looks for co-dependent PRs where all are ready, could be something
  along the lines of::

      (any(priority = 0) and every(state != error)) or every(state = ready)

TODO
----

* PR edition (retarget, title/message)
* Ability to disable/ignore branches in runbot (tmp branches where
  staging is being built)
* What happens when cancelling staging during bisection

TODO?
-----

* Prioritize urgent PRs over existing batches?
* Make autosquash dynamic? Currently PR marked as squash if only 1
  commit on creation, this is not changed if more commits are added.
* Use actual GH reviews? Currently only PR comments count.
* Rebase? Not sure what use that would have & would need to be done by
  hand

Structure
---------

A *project* is used to manage multiple *repositories* across many
*branches*.

Each *PR* targets a specific branch in a specific repository.

A *batch* is a number of co-dependent PRs, PRs which are assumed to
depend on one another (the exact relationship is irrelevant) and thus
always need to be batched together. Batches are normally created on
the fly during staging.

A *staging* is a number of batches (up to 8 by default) which will be
tested together, and split if CI fails. Each staging applies to a
single *branch* the target) across all managed repositories. Stagings
can be active (currently live on the various staging branches) or
inactive (to be staged later, generally as a result of splitting a
failed staging).

Notes
-----

* When looking for stageable batches, priority is taken in account and
  isolating e.g. if there's a single high-priority PR, low-priority
  PRs are ignored completely and only that will be staged on its own
* Reviewers are set up on partners so we can e.g. have author-tracking
  & deletate reviewers without needing to create proper users for
  every contributor.
* MB collates statuses on commits independently from other objects, so
  a commit getting CI'd in odoo-dev/odoo then made into a PR on
  odoo/odoo should be correctly interpreted assuming odoo-dev/odoo
  sent its statuses to the MB.
* Github does not support transactional sequences of API calls, so
  it's possible that "intermediate" staging states are visible & have
  to be rollbacked e.g. a staging succeeds in a 2-repo scenario,
  A.{target} is ff-d to A.{staging}, then B.{target}'s ff to
  B.{staging} fails, we have to rollback A.{target}.
* Batches & stagings are non-permanent, they are deleted after success
  or failure.
* Co-dependence is currently inferred through *labels*, which is a
  pair of ``{login}:{branchname}``
  e.g. odoo-dev:11.0-pr-flanker-jke. If this label is present in a PR
  to A and a PR to B, these two PRs will be collected into a single
  batch to ensure they always get batched (and failed) together.

Previous Work
-------------

bors-ng
~~~~~~~

* r+: accept (only for trusted reviewers)
* r-: unaccept
* r=users...: accept on behalf of users
* delegate+: allows author to self-review
* delegate=users...: allow non-reviewers users to review
* try: stage build (to separate branch) but don't merge on succes

Why not bors-ng
###############

* no concurrent staging (can only stage one target at a time)
* can't do co-dependent repositories/multi-repo staging
* cancels/forgets r+'d branches on FF failure (emergency pushes)
  instead of re-staging
* unclear whether prioritisation supported

homu
~~~~

Additionally to bors-ng's:

* SHA option on r+/r=, guards
* p=NUMBER: set priority (unclear if best = low/high)
* rollup/rollup-: should be default
* retry: re-attempt PR (flaky?)
* delegate-: remove delegate+/delegate=
* force: ???
* clean: ???
