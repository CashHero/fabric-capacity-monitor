import pytest

from fabric_capacity_monitor.rates import Rates


@pytest.fixture(scope="session")
def rates() -> Rates:
    return Rates.load()
