=========
Merge Bot
=========

The mergebot is Odoo's internal tool for merging PRs.

It is conceptually rooted in Graydon Hoare's `"not rocket science" rule of
software engineering`_:

    "automatically maintain a repository of code that always passes all the tests"

That is not only should PRs pass tests before being merged, PRs should be tested
*as integrated*, in order to avoid logical conflicts (two PRs which pass tests
independently but do not pass when merged together, or the PR was tested with
an older version of the target branch but doesn't work with the latest).

This idea has become a lot more popular and democratized with the advent of
gitlab's `merge trains`_ and github's `merge queues`_.

Justification of Existence
==========================

Although merge queues exist for the general public, they are unsuitable for Odoo
for a number of reasons:

- Odoo development has multi-repo dependencies, which merge queues simply do not
  support (as of rewriting this readme, an odoo change can involve up to 6
  repositories, some of which are co-dependent). This is by far the biggest
  ticket as it makes merge queues a non-contender.
- Github access rights are pretty rough, especially when using the old pricing
  model (which limits roles to admin, write, and read).
- Some cross-repository tasks should be synchronised with staging (e.g.
  freezing, patching), though they could always just fuck with the queue.
- Github does not support forward- or back-ports internally, which still
  requires extra tooling to manage this (as well as ACLs on the ports).

Development
===========

Because the mergebot has significant interactions with github (via git, the
github API, and webhooks from github) it is not trivial to test. Not only that,
but although it is possible to run the test suite against github actual because
of the rate limits (primary and secondary) it takes literally days to go through
the 500-odd integration tests [#github]_.

As a result, the mergebot is mostly tested locally, which requires its specific
setup and tooling.

Shared Setup
------------

Although the mergebot might be testable in addons-path layout, it has only been
tested with a ``PYTHONPATH`` layout, that is::

    /
    ├── odoo           ── odoo/odoo checkout here
    ├── community
    │   └── odoo
    │       └── addons -> ../../odoo/addons
    ├── enterprise
    │   └── odoo
    │       └── addons ── odoo/enterprise checkout here
    └── runbot
        └── odoo
            └── addons ── odoo/runbot checkout here

and using ``PYTHONPATH`` to add the addon directories to odoo via python
namespaces (rather than ``--addons-path``).

This also requires [#pytest.ini]_ a ``pytest.ini`` file such that pytest can know
the project root (``rootpath``), and because the rootpath is not inside any
repository (it's at the ``/`` in the block above) it has to be created by hand.

A working sample file currently is:

.. include:: ../pytest.ini.sample
   :code: ini

.. hint::

    Assuming the sample works out of the box, you can just create a symlink to
    that sample file from the root::

        ln -s runbot/odoo/addons/pytest.ini.sample pytest.ini

    Although you may not want to do that if you need local changes e.g. remove
    the ``addopt`` because you want to change defaults or not use xdist
    implicitly.

.. [#pytest.ini] does it still?

Dependencies
''''''''''''

Aside from Odoo's own dependencies (``odoo/requirements.txt`` plus a few more
secrets hidden for fun) runbot_merge has a few requirements:

- ``markdown``
- ``mergiraf`` is an optional dependency but some tests check for it
- ``sentry_sdk`` should be removed one day
- ``pytest`` for running the test suite
  With a few strongly recommended plugins, especially if using the sample file:
  - ``pytest-timeout``
  - ``pytest-xdist``
  - ``pytest-sugar`` (not needed at all but provides nice options)
- ``mitmproxy`` to test locally, probably easier to install globally
- ``cssselect`` is used by the test suite

Local testing
-------------

The test suite should mostly be run locally as it can be run at a much higher
clip, and concurrently. However this requires some setup as the test harness was
kept (mostly) unaware of the difference to limit the risk of divergences from
github actual which absolutely plagued the previous mock-based version).

Thus running the test suite locally requires:

- mitmproxy_ (follow instructions to set up locally), which is used to redirect
  requests to Github (from both the test harness and the running Odoo instances)
  to
- dummy-central_, a simulator of the github API (mostly implementing the stuff
  which is or used to be needed to run the test suite).

  The mergebot was originally tested using mocks, but the asynchronous nature
  of various operations and the difficulty of differential testing made hewing
  close to github's behaviour impossible, leading to features working with mocks
  but needing to be pretty much entirely rewritten when testing against github
  actual, and thus the mock unit tests being largely useless.

  Dummy central has made local testing an infinitely better predictor of github
  behaviour, with new tests mostly working out of the box against github once
  they work against DC.

.. warning::

    Requests has its own CA bundle, so after setting up mitmproxy's CA you have
    to make it use the system CA bundle via ``REQUESTS_CA_BUNDLE``. Assuming you're
    using a ``.env`` file to configure your ``PYTHONPATH`` this is a fine location to
    do that.

Configuration
'''''''''''''

The test suite *should* be run with ``pytest-xdist`` installed [#perf]_::

    pytest -n logical --users $USERS_FILE

``USERS_FILE``
    The description of the users roles / github users / github organisation,
    see `users file`_.

.. note:: ``logical`` runs an xdist worker per thread which seems to work well
            on a Ryzen 5 7530U (6 cores 12 threads) [#numprocesses]_.

Autoconf
~~~~~~~~

As long as ``mitmdump`` and ``dummy_central`` are on the ``PATH``, the test
suite will run them internally as needed. This should work out of the box.

Manual Setup
~~~~~~~~~~~~

In some cases (e.g. trying to update or debug dummy central, running with
tracing, ...) it can be necessary to run the test suite with externally setup
mitmproxy / dummy-central.

In those cases:

- dummy-central *must not* be on the path
- dummy-central should probably be run from a git checkout and started manually::

      cargo run --release -- $USERS_FILE -p $PORT

- mitmproxy should be run as::

      mitmdump -M '|^https?://(\w+\.)?github.com/|http://localhost:$PORT/'

  where ``$PORT`` is the port to which dummy-central is bound. It should also
  have the following configuration keys set (in its config file, or via
  ``--set`` parameters): ``upstream_cert: false`` and
  ``connection_strategy: lazy``. Missing *either* of those causes mitmproxy to
  connect to github.com unnecessarily, which

  - consumes github resources for no reason
  - causes flakiness in the local test suite when the connection fails (e.g.
    because github or the network is down)
  - slows mitmproxy down

- the test suite *must* be run with the proxy specified::

      HTTPS_PROXY=$PROXY_ADDR pytest -n logical --users $USERS_FILE

  ``PROXY_ADDR``
      The address on which mitmproxy is bound, defaults to
      ``http://localhost:8080`` unless ``--listen-port`` (``-p``) is used.

Users File
''''''''''

This is a JSON file which serves to:

- Tell dummy-central what github-level organisations and users to expose.
- Tell the test suite what these users are (when communicating with
  dummy-central, or github actual) and how they map to internal *roles*.

It should be shared between dummy-central and the test suite to ensure
consistent behaviour. The users file is a JSON object, where each key is a
github identifier (login), and each value is an object with the following
entries:

``type``
    Can be either ``Organization`` or ``User``. Only the ``owner`` entry should
    be an ``Organization``.
``role``
    The role of the user in the test suite, used to avoid addressing entities
    in a way which is non-portable.

    ``user`` (mandatory)
        Is the primary actor, used as the bot user, and should have write access
        to the default repository location, which is either implicit (itself)
        or the ``owner`` organisation.
    ``owner`` (optional)
        The location where repositories are created by default, if present
        should be an organisation, otherwise repositories are created in
        ``user``'s account [#owner]_.
``email`` (optional)
    The email address of the entity, mostly used to compose git authorship.
    information.
``token``
    An array of access tokens, the test suite will use the first of each array
    to access github. The ``owner`` does not need (and can not use) a token.

The users file needs to have *at least* 4 entries, if none has the ``owner``
role. If one has the ``owner`` role then it needs *at least* 5 entries.

Github actual
-------------

Running against github actual is not much less involved as it needs both several
*actual* user accounts, and a tunnel to hook webhooks back in.

Tunnels
'''''''

The test suite has first-class support for tunnels via the ``--tunnel``
parameter which should link to a tunnel script. The repository bundles tunnel
scripts for ``ngrok``, ``localtunnel``, and ``cloudflared`` e.g.::

    pytest --tunnel runbot/odoo/addons/runbot_merge/ngrok

should automatically create and teardown tunnels from github, and create
webhooks to the remote end of the tunnel.

A tunnel script simply takes the local port as first argument, it should then:

- establish a tunnel to that port on stdout
- wait for the tunnel to be available if needed
- write the tunnel to stdout, followed by a newline
- flush stdout
- close stdout **ensuring the underlying fd is properly closed** e.g. in python
  standard streams are wrapped with ``closefd=False`` so it's necessary to
  ``os.close`` the underlying ``fileno()``
- wait until SIGTERM (test suite) or SIGINT (Ctrl+C from a terminal)
- close the tunnel and exit

Accounts
''''''''

As the test suite itself requires multiple accounts, they also have to be
provided when running against github actual, with a similar setup to
`local testing`_, although:

- the top-level keys (organisation and user logins) need to be valid github accounts
- the TODO fields are not used
- The accounts need to be legitimate

  - While github has become more accepting of multiple user accounts (with an
    integrated account switcher), accounts should be ramped up slowly lest
    they be flagged as spam.

    This is especially problematic for the non-``user`` roles here as their
    near entire activity is leaving comments, which is a very spammy workload.
  - Although attractive due to easier handling, the ``user`` role probably
    should not be your primary github account: the main brake on running the
    test suite is the secondary rate limit on pull requests creation [#git]_,
    and once it is hit you will not be able to open PRs for several hours
    *with that entire account*, including via the web UI and the online
    vscode.
- The personal tokens neeed to be valid

  - Every user token needs ``public_repo`` and ``delete_repo``.
  - The ``user`` role further needs ``admin:repo_hook`` and ``user:email`` (?).
- xdist *may not* be enabled, github's TOS / API recommendation strongly
  request no concurrency, and this will just trigger rate limits faster.
- The test suite should be run in ``--sw`` (stepwise): it will stop at the first
  failure (like ``-x``), but will then resume from the failed case, which allows

  - Resuming after fix in case of true negative, limiting the need for duplicat
    test runs (not an issue normally, but a major concern with rate limiting).
  - Resuming immediately in case of false negative (e.g. concurrency / timeout
    issue).
  - Waiting a few hours in case of secondary rate limit failure (generally
    translating to).

.. warning::

    If trying to both run the test suite against github actual and work with
    local tests at the same time, it is necessary to use ``-o cache_dir``:
    pytest's cache feature (which is used by stepwise, last-failed,
    failed-first, etc...) is not compatible with concurrent runs so the two runs
    will influence one another and generate strange run states.

    It is likely preferrable for the run against github actual to use a bespoke
    ``cache_dir``, and for the "default" cache to be used by the (much faster
    and more reliable) local test runs.

    See `configuration options: cache_dir
    <https://docs.pytest.org/en/stable/reference/reference.html#confval-cache_dir>`_
    for more details.

Tracing
-------

Although assertions and logs usually make errors easy to diagnose, pipe
(between the pytest test harness and the odoo process under test) and back and
forth async operations (between the running odoo and dummy-central) can make
understanding some issues difficult.

To that end, tracing support was added to the test harness and to dummy central:

- install pytest-opentelemetry (in the same environment as pytest)
- setup an opentelemetry client / sink of some sort [#otel]_.
- run dummy-central with ``--features otlp``, and the environment
  ``OTEL_SERVICE_NAME=dummy-central OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317``
- run the test suite with the parameters ``--export-traces --trace-per-test``

This should export a trace for each test, with the entire test content
(including Odoo and dummy-central paths, to the extent that they have spans
defined either automatically or explicitly) nested under it.

.. warning:: it looks like events don't carry over properly from tokio-tracing
             to opentelemetry

.. note::

    One oddity to the setup is that webhook calls are nested within the
    operation which triggered them, even though they are performed
    asynchronously at some later time.

    This is because I was not able to get `Span::follows_from`__ or
    `OpenTelemetrySpanExt::add_link`__ to work correctly (or at least to show
    up in any way in `otel desktop viewer`_ or venator_.

__ https://docs.rs/tracing/latest/tracing/struct.Span.html#method.follows_from

__ https://docs.rs/tracing-opentelemetry/latest/tracing_opentelemetry/trait.OpenTelemetrySpanExt.html#tymethod.add_link

.. _"not rocket science" rule of software engineering:
        https://graydon2.dreamwidth.org/1597.html
.. _merge trains: https://docs.gitlab.com/ci/pipelines/merge_trains/
.. _merge queues:
        https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue
.. _mitmproxy: https://mitmproxy.org/
.. _dummy-central: https://github.com/odoo/dummy-central
.. _#34: https://github.com/chrisguidry/pytest-opentelemetry/pull/34
.. _otel desktop viewer: https://github.com/CtrlSpice/otel-desktop-viewer
.. _venator: https://github.com/kmdreko/venator

.. [#github]
    As of writing this document, the test suite against github took about 15
    hours wallclock, however it was interrupted 9 times between github
    misbehaving, the test suite not necessarily being 100% reliable, and having
    to go home for the day(s).

    .. 1 failed, 37 passed, 1 skipped in 7954.84s (2:12:34)
       1 failed, 62 passed, 38 deselected in 12032.05s (3:20:32)
       1 failed, 35 passed, 100 deselected in 6350.45s (1:45:50)
       1 failed, 123 passed, 4 skipped, 135 deselected, 2 xfailed in 10389.24s (2:53:09)
       1 failed, 16 passed, 264 deselected in 1619.17s (0:26:59)
       1 failed, 3 passed, 280 deselected in 374.17s (0:06:14)
       1 failed, 5 passed, 283 deselected in 1011.14s (0:16:51)
       1 failed, 17 passed, 2 skipped, 288 deselected in 1682.49s (0:28:02)
       1 failed, 7 passed, 307 deselected in 583.26s (0:09:43)
       159 passed, 3 skipped, 314 deselected in 13287.71s (3:41:27)
.. [#git] especially since the switch to using git directly, as the mergebot's
          need to call the rate-limited github API has gone down
          *significantly*: as of writing this it's down to ~1500 calls/h whe
          running the test suite, ~300/h in production.
.. [#perf]
    Although it is not required, because of all the waiting around to leave time
    for webhooks to be dispatched the test suite has a pretty low level of
    average CPU utilisation (about 50%, of one core), and thus takes *ages* to
    complete. Using xdist is a very easy way to fill up the CPU with work, and
    thus complete the test suite faster.
.. [#owner] well that's how it's supposed to work anyway, I'm not sure I've ever
            tested it...
.. [#numprocesses]
    It does trigger the odd false negative but does so at a very reasonable
    (low) rate. ``auto`` (worker per core) *might be slightly more reliable, but
    on my current machine it mostly leads to a significant slowdown in wallclock
    run time, from::

        3 failed, 461 passed, 10 skipped, 2 xfailed in 1220.60s (0:20:20)
        5436.98s user 901.32s system 518% cpu 20:22.01 total

    to::

        2 failed, 462 passed, 10 skipped, 2 xfailed in 1972.72s (0:32:52)
        3687.33s user 671.44s system 220% cpu 32:54.30 total

.. [#otel]
    Some options are Jaeger, Grafana Tempo, Signoz, OpenObserve, or cloud
    solutions like Datadog, New Relic, Honeycomb.

    venator_ is a useful desktop sink, it should be run as::

        venator -d :memory: -b localhost:4317

    for otel compatibility
