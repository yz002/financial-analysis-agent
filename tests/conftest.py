import pytest

from src.analysis.statements import get_statement
from src.data.edgar_client import EdgarClient


@pytest.fixture(scope="session")
def client():
    return EdgarClient()


@pytest.fixture(scope="session")
def msft_quarterly(client):
    return get_statement("MSFT", "quarterly", client=client)


@pytest.fixture(scope="session")
def nvda_quarterly(client):
    return get_statement("NVDA", "quarterly", client=client)


@pytest.fixture(scope="session")
def ford_quarterly(client):
    return get_statement("F", "quarterly", client=client)


@pytest.fixture(scope="session")
def msft_annual(client):
    return get_statement("MSFT", "annual", client=client)
