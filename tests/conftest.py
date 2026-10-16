import pytest


@pytest.fixture(autouse=True)
def _no_folder_settling(monkeypatch):
    # The guard looks again for a folder it didn't find, a second or two later, before making one (Stroom's explorer
    # tree lags): no fake here lags, so no test waits. test_guard covers the looking again.
    monkeypatch.setattr('security.guard._SETTLE', ())


@pytest.fixture(autouse=True)
def _one_event_logging_version(monkeypatch):
    # The fakes hold no XMLSchema docs: Stroom holds event-logging 4.1.0 alone here (the fixtures' newest), so no test
    # is asked which version to write. test_event_versions covers the asking.
    async def installed(self):
        return ['4.1.0']
    monkeypatch.setattr('utils.schemas.SchemaCache.event_versions', installed)


@pytest.fixture(autouse=True)
def _schema_locations_unjudged(monkeypatch):
    # Nor can the fakes say which system ids Stroom holds: check_xslt judges no schema location here.
    # test_schema_locations covers the judging.
    async def none(self, root):
        return []
    monkeypatch.setattr('utils.schemas.SchemaCache.location_problems', none)
