def pytest_configure(config):
    config.addinivalue_line("markers", "airsim_mode(mode): how airsim:// inputs are fetched in cluster tests")
