"""Screen filters become SQL column names, so anything not in the catalogue must be refused
before a query is built."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_unknown_filter_field_is_refused():
    r = client.get("/api/v1/companies", params={"f": "mcap;drop table x:gt:1"})
    assert r.status_code == 400


def test_unknown_sort_field_is_refused():
    assert client.get("/api/v1/companies", params={"sort": "1; select pg_sleep(5)"}).status_code == 400


def test_bad_symbol_is_refused():
    assert client.get("/api/v1/companies/a'b").status_code == 400


def test_bad_operator_is_refused():
    assert client.get("/api/v1/companies", params={"f": "roce:like:1"}).status_code == 400
