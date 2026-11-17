Before submitting a patch, ensure that:

* `./lint`, run from the root folder, completes successfully
* `python test_upstream.py` runs successfully
* `python profile.py` looks reasonable (use `python profile.py --synthetic` if you don't have a better test case to measure)