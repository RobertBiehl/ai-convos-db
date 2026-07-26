import gc


def pytest_runtest_teardown(): gc.collect()
