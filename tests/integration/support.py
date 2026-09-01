"""Shared constants for the integration suite.

Deliberately *not* in ``conftest.py``. Importing a conftest module by name loads
it a second time as an ordinary module, so its session-scoped fixtures exist
twice; one copy's teardown then drops the disposable database while a test using
the other copy is still connected. That failure looks like a random
"database ... does not exist" partway through the run.
"""

from __future__ import annotations

# Fail fast when PostgreSQL is not listening. Without this, every test waits out
# the driver's default connect timeout, turning "the database is down" into a
# four-minute run instead of a four-second one.
CONNECT_ARGS = {"connect_timeout": 5}
