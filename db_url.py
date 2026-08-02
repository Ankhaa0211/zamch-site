"""Normalize DB URLs for local SQLite and Railway Postgres."""


def normalize_database_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return "sqlite:///./database.db"
    # Railway / Heroku style
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://") :]
    # SQLAlchemy + psycopg3 driver
    if value.startswith("postgresql://") and "+psycopg" not in value.split("://", 1)[0]:
        value = "postgresql+psycopg://" + value[len("postgresql://") :]
    return value
