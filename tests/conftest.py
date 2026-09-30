import time

import pytest


@pytest.fixture(autouse=True)
def _tzset_after_test():
    """Re-read the timezone database after every test.

    TZ-pinning tests (humanize_age / age_bucket local-calendar checks) set
    the TZ env var and call time.tzset(); if such a test dies mid-run the
    process would keep the pinned zone and poison later tests' local-date
    maths. tzset() after each test re-reads the (restored) TZ — idempotent,
    and POSIX-only like the rest of the suite.
    """
    yield
    time.tzset()
