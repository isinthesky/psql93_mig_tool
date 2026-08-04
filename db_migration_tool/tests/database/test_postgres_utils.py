from src.database.postgres_utils import PostgresOptimizer


class DummyCursor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *_args, **_kwargs):
        pass

    def fetchone(self):
        return (1,)


class DummyPsycopgConnection:
    def __init__(self):
        self.closed = False

    def cursor(self):
        return DummyCursor()

    def close(self):
        self.closed = True


def test_check_connection_quick_defaults_missing_port(monkeypatch):
    captured = {}

    def fake_connect(**params):
        captured.update(params)
        return DummyPsycopgConnection()

    monkeypatch.setattr("psycopg.connect", fake_connect)

    ok, msg = PostgresOptimizer.check_connection_quick(
        {
            "kind": "postgres",
            "host": "localhost",
            "database": "db",
            "username": "user",
            "password": "pass",
        }
    )

    assert ok is True
    assert msg
    assert captured["port"] == 5432
