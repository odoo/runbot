Execute this test suite using pytest.

The default mode is to run tests locally using mock objects in place of
github, see the docstring of remote.py for additional instructions to run
against github "actual".

Shared properties running tests, regardless of the github implementation:

* test should be run from the root of the runbot repository providing the
  name of this module aka ``pytest runbot_merge`` or
  ``python -mpytest runbot_merge``
* a database name to use must be provided using ``--db``, the database should
  not exist beforehand
* the addons path must be specified using ``--addons-path``, both "runbot" and
  the standard addons (odoo/addons) must be provided explicitly

See pytest's documentation for other options, I would recommend ``-rXs``,
``-v`` and ``--showlocals``.

When running "remote" tests as they take a very long time (hours) `-x`
(aka ``--maxfail=1``) and ``--ff`` (run previously failed first) is also
recommended unless e.g. you run the tests overnight.
