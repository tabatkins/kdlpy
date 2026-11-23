Before submitting a patch, ensure that:

* `./lint`, run from the root folder, completes successfully (runs linters, testsuite, and perf checks)

Testing
=======

This library uses `pytest` for testing. 
If installed properly via `requirements-dev.txt`, just running `pytest` will execute all tests.
(This is also done automatically as part of the lint script.)
Run `pytest -h` or check the PyTest docs for help.

When prudent, add new tests either to one of the existing test scripts under `tests/`, 
or a new test script.

Do not alter the `upstream_tests` folder or its contents;
those come from the official KDL repository and represent the canonical testsuite.
They're tested by `tests/test_upstream.py`.