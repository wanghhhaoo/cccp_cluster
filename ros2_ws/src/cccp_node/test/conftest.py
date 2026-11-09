def pytest_configure(config):
    config.addinivalue_line("markers", "airsim_mode(mode): how airsim:// inputs are fetched in cluster tests")
    config.addinivalue_line("markers", "input_timeout(seconds): executor input_timeout in cluster tests")
    config.addinivalue_line("markers", "executor_status_period(seconds): ExecutorStatus timer period in cluster tests")
