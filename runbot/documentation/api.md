# Runbot API

Read-only JSON API of the runbot frontend.
A bundle is a branch name across repos, a batch is one run of it on a set of commits,
a build is one test of a batch (one per trigger) and can have child builds.

## Routes

- `GET /runbot/api/bundle/<ids_or_names>?project_id=1`: bundles by id, slug (`master-1`) or name (`master`), with their last batch.
  The same name can exist in multiple projects, `project_id` (optional) restricts the result to one of them.
  `batch_history=10` adds their 10 last batches (40 at most), of all categories, in `batches`.
- `GET /runbot/api/bundle?pr=odoo:297417`: bundles of pull requests, by number, optionally prefixed by the repo name.
  A number without repo can match pull requests of several repos. When `pr` is given, ids and names are ignored.
- `GET /runbot/api/batch/<ids>?depth=1&logs=errors`: batches with their commits, logs and slots (one per trigger, with its build).
- `GET /runbot/api/build/<ids>?depth=1&logs=errors`: builds with their state, commits, logs, jobs, log_files, databases and children.
- `GET /runbot/api/commit/<ids_or_shas>`: commits by id, slug (`odoo-d14d38cc-123456`) or full sha, with their last status for each context, as on the commit page.
  It can be more up to date than GitHub: a status with `sent_date` false was not sent to GitHub yet.
  The same sha can exist in multiple repos or be rebased on different commits, so a sha can return multiple commits.
- `GET /runbot/api/errors?sort=last_seen_date desc&limit=20&offset=0`: open errors seen in more than one build, as on the errors page.
  `sort` is `last_seen_date`, `first_seen_date` or `build_count`, followed by `desc` or `asc`.
- `GET /runbot/api/error/<ids>`: errors (open or fixed) with their PRs, versions and contents (one per distinct error message).
  For each version the error was seen in: the last done batch of the same bundle and category before it appeared
  (`previous_batch_id`, compare its commits with the first seen batch to find what broke), the first and last seen batches,
  the first done batch after it was last seen (`next_batch_id`, false if none ran since: the error may still happen) and the number of
  builds where it was seen per day over the last 30 days (`daily_counts`).

## Conventions

- Endpoints take a comma separated list of ids (or names) and return a JSON list.
- Records reference each other by id, fetch them with the matching route.
- A key missing on a child build has the same value as on its parent (e.g. trigger, version, commits).
- A trigger is given as `id`, `name` and `purpose` (only when set), `triggers` is a list of them.
  The errors list only gives the trigger names, use the error route for the details.
- `canonical_tag` on a log line is the test tag of the test that logged it, if any, to run this test alone.
- `known_error` on a log line is the already known runbot error matching it (false if none), with when it was first
  and last seen, whether it is still open (`active`, false once fixed) and if it looks random (`random`).
  `random` comes from a heuristic: false only means the error was not detected as random, not that it isn't.
  Errors are only linked to nightly builds, so `last_seen_date` can be in the past even if the error still happens:
  it may be fixed, the last nightly may not be finished, or a random error may not have been reproduced in it.
- Errors are only given for the projects the user can see: logging in is required for projects restricted to some groups.
  `assigned` tells if someone is responsible for fixing the error, `responsible_github_login` gives their GitHub login if known.
  The `limit` of the errors route has the same maximum as the number of ids.
- `reference_build_id` is the build whose database a build restores, when known from its parameters:
  its `databases` give the download links.
  Builds can also restore a database chosen at run time, see the restore step in their logs.
- Builds requested with the build route also have the `bundle_id` and `bundle_name` of their `create_batch_id`.
  A build can be reused in other batches and bundles than the one that created it.
- `build_time` and `docker_time` are in seconds and only cover the build itself, not its children.
- `jobs` are the steps run by the build, in order. Their times are in milliseconds: `duration` (wall time, 0 while running
  and for running steps), `python_time` (preparation, including `git_time` and `docker_pull_time`), `docker_time` and `result_time`.
- `last_batch` of a bundle is its last batch of the default category (false if none), with its commits and the `global_state`
  of its least advanced build (false if no build yet): a batch is `done` once all its builds are finished,
  and stays `done` while a build is rebuilt.
  The batches of `batches` only have their id, state, result, category and create_date: fetch them with the batch route.
- Batch logs only include the non-INFO ones.
- `slots` of a batch give the trigger and its `build` (false if none was created, `manual` triggers without build are not listed).
  `link_type` is `created`, `matched` (build reused from another batch) or `rebuild`. The build of a slot has no `trigger` key
  when it is the trigger of the slot.
- Dates are UTC. Log `create_date` and job `start_date` have milliseconds, to place the logs within the jobs.

## Parameters

- `depth`: levels of children to include (0: none, default 1).
  Children past the depth have an empty `children` list, fetch them by id.
- `logs`: build log lines to include: `errors` (warnings and errors, default), `all` or `none`.
  The full text logs are linked in `log_files`, one per step. Each starts with `Docker command:`, then the main command
  run by the step on a single line (addons path, database, modules, test tags), then a line of `=`.
  The command can be long with big module or test lists: reading the first 64 KB (`Range: bytes=0-65535`)
  is suggested to get the full command in most cases.

## Values

- states: pending, testing, waiting, running, done
- results: ok, warn, ko, skipped, killed
- `global_state`/`global_result` include the children, `local_state`/`local_result` do not.
