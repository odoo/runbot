Merge Bot
=========

Odoo workflow
-------------

The sticky branches are protected on the github odoo project to restrict
push for the Merge Bot (MB) only.

The MB only works with PR's using the github API.

1. When a PR is created the github notifies the MB. The MB labels the PR
   as 'seen 🙂' on github [#]_.

2. Once the PR github statuses are green [#]_ , the MB labels the PR as
   'CI 🤖'.

3. When a reviewer, known by the MB, approves the PR, the MB labels that
   PR as 'r+ 👌'.

4. At this moment, MB tries to merge the PR and labels the PR with
   'merging 👷'.

5. If the merge is successfull, MB labels it 'merged 🎉', removes the
   label 'merging 👷' and closes the PR. A message from MB gives a link
   to the merge's commit [#]_.

If an error occurs during the step 4, MB labels the PR with 'error 🙅'
and adds a message in the conversion stating what kind of error.  For
example 'Unable to stage PR (merge conflict)'.

If a new commit is pushed in the PR, the process starts again from the
begining.

It's possible to interact with the MB by the way of github messages
containing `Commands`_. The message must start with the MB name (for
instance 'robodoo').

.. [#] Any activity on a PR the MB hasn't seen yet will bring it to the
   MB's attention. e.g a comment on a PR.

.. [#] At this moment the statuses are: Runbot build is green and CLA is
   signed if needed.  The expected statuses may change in the future.

.. [#] If a PR contains only one commit, the PR is rebased and the
   commit is fast forwarded. With more than one commit, the PR is
   rebased and the commits are merged with a merge commit. When one
   wants to avoid the rebase, 'rebase-' command should be used.

Setup
-----

* Setup a project with relevant repositories and branches the bot
  should manage (e.g. odoo/odoo and 10.0).
* Set up reviewers (github_login + boolean flag on partners).
* Add "Issue comments", "Pull request reviews", "Pull requests" and
  "Statuses" webhooks to managed repositories.
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

1. for each active staging, check if they are done

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
   * otherwise look for batches targeted to that PR (PRs grouped by
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

  submitting an "approve" review implicitly r+'s the PR

r(eview)-
  removes approval from a PR, allows un-reviewing a PR in error (staging
  failed) so it can be updated and re-submitted

.. squash+/squash-
..   marks the PR as squash or merge, can override squash inference or a
..   previous squash command, can only be used by reviewers

delegate+/delegate=<users>
  adds either PR author or the specified (github) users as authorised
  reviewers for this PR. ``<users>`` is a comma-separated list of
  github usernames (no @), can be used by reviewers

p(riority)=2|1|0
  sets the priority to normal (2), pressing (1) or urgent (0),
  lower-priority PRs are selected first and batched together, can be
  used by reviewers

rebase-
  the default merge mode is to rebase and merge the PR into the
  target, however for some situations this is not suitable and
  a regular merge is necessary; this command toggles rebasing
  mode off (and thus back to a regular merge)

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
  & delegate reviewers without needing to create proper users for
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
* Co-dependence is currently inferred through *labels*, which is a
  pair of ``{repo}:{branchname}`` e.g. odoo-dev:11.0-pr-flanker-jke.
  If this label is present in a PR to A and a PR to B, these two
  PRs will be collected into a single batch to ensure they always
  get batched (and failed) together.

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
