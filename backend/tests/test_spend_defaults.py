from hundred_acre.spend import db as spend_db


def test_spend_defaults_point_at_the_hundred_acre_server(monkeypatch):
    monkeypatch.delenv("SPEND_DATABASE_URL", raising=False)
    monkeypatch.delenv("SPEND_TEST_SERVER_URL", raising=False)
    assert spend_db.spend_database_url().endswith("@localhost:5433/hundred_acre_spend")
    assert spend_db.server_admin_url().endswith("@localhost:5433/postgres")
