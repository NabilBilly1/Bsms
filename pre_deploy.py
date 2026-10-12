"""
pre_deploy.py — Run before every Railway deploy.

Logic:
  1. Connect to the DATABASE_URL.
  2. If 'alembic_version' table is missing OR has no rows → stamp to
     revision 33429ad950d3 (the last migration that existed before this
     script was added).  This handles the case where the database was
     built from raw SQLAlchemy create_all() and has no migration history.
  3. Run 'alembic upgrade head' so any new migrations are applied.
"""
import os
import subprocess
import sys

from sqlalchemy import create_engine, inspect, text

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if not DATABASE_URL:
    print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
    sys.exit(1)

engine = create_engine(DATABASE_URL)

with engine.connect() as conn:
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    has_version_table = "alembic_version" in tables
    version_row = None
    if has_version_table:
        version_row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()

if not has_version_table or version_row is None:
    # Database has no migration history — stamp it to the last old revision
    # so that upgrade head only runs the NEW migrations on top.
    STAMP_REVISION = "33429ad950d3"
    print(f"No alembic version found. Stamping database to {STAMP_REVISION} ...")
    result = subprocess.run(
        ["alembic", "stamp", STAMP_REVISION],
        check=True
    )
else:
    print(f"Alembic version already set to: {version_row[0]}")

print("Running alembic upgrade head ...")
subprocess.run(["alembic", "upgrade", "head"], check=True)
print("Migration complete.")
