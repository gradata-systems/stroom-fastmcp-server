import pytest


@pytest.fixture(autouse=True)
def _no_folder_settling(monkeypatch):
    # The guard looks again for a folder it didn't find, a second or two later, before making one (Stroom's explorer
    # tree lags): no fake here lags, so no test waits. test_guard covers the looking again.
    monkeypatch.setattr('security.guard._SETTLE', ())
