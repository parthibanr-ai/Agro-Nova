"""Run the database migrations: `python -m app.migrate`.

Run this once per release (for example as a Cloud Run job or a deploy step) before starting new instances, with
AUTO_MIGRATE=false on the instances themselves.
"""

import logging

from app.db import DATABASE_URL, migrate

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    target = DATABASE_URL.split("@")[-1]  # never print credentials
    logging.getLogger(__name__).info("Migrating %s", target)
    migrate()
    logging.getLogger(__name__).info("Database is up to date")
