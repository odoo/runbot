# Runbot API

Read-only JSON API of the runbot frontend.
A bundle is a branch name across repos, a batch is one run of it on a set of commits,
a build is one test of a batch (one per trigger) and can have child builds.

## Routes

- `/runbot/api/bundle/<ids_or_names>?project_id=1`: bundles by id, slug (`master-1`) or name (`master`), with their 40 last batches.
  The same name can exist in multiple projects, `project_id` (optional) restricts the result to one of them.
- `/runbot/api/bundle?pr=odoo:297417`: bundles of pull requests, by number, optionally prefixed by the repo name.
  A number without repo can match pull requests of several repos. When `pr` is given, ids and names are ignored.
- `/runbot/api/batch/<ids>?depth=1&logs=errors`: batches with their commits, logs and slots (one per trigger, with its build).
- `/runbot/api/build/<ids>?depth=1&logs=errors`: builds with their state, commits, logs, log_files, databases and children.

## Conventions

- Endpoints take a comma separated list of ids (or names) and return a JSON list.
- Records reference each other by id, fetch them with the matching route.
- A key missing on a child build has the same value as on its parent (e.g. trigger, version, commits).
- `trigger_purpose` is only given along with `trigger`, when the trigger has one.
- `canonical_tag` on a log line is the test tag of the test that logged it, if any, to run this test alone.
- `known_error` on a log line is the already known runbot error matching it (false if none), with when it was first
  and last seen, whether it is still open (`active`, false once fixed) and if it looks random (`random`, not fully reliable yet).
  Errors are only linked to nightly builds, so `last_seen_date` can be in the past even if the error still happens:
  it may be fixed, the last nightly may not be finished, or a random error may not have been reproduced in it.
- `reference_build_id` is the build whose database a build restores, when known from its parameters:
  its `databases` give the download links.
  Builds can also restore a database chosen at run time, see the restore step in their logs.
- Builds requested with the build route also have the `bundle_id` and `bundle_name` of their `create_batch_id`.
  A build can be reused in other batches and bundles than the one that created it.
- `build_time` and `docker_time` are in seconds and only cover the build itself, not its children.
- Batch logs only include the non-INFO ones.
- `slots` of a batch give the trigger and its `build` (false if none was created, `manual` triggers without build are not listed).
  `link_type` is `created`, `matched` (build reused from another batch) or `rebuild`. The build of a slot has no `trigger` key
  when it is the trigger of the slot.
- Dates are UTC.

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
