import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("No DATABASE_URL found in .env")
    exit(1)

engine = create_engine(DATABASE_URL)

def migrate():
    with engine.connect() as conn:
        print("Checking and adding missing columns...")
        try:
            # Add reporter_id
            conn.execute(text("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS reporter_id VARCHAR;"))
            # Add in_progress_at
            conn.execute(text("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS in_progress_at TIMESTAMP;"))
            conn.commit()
            print("Migration successful: Columns added.")
        except Exception as e:
            print(f"Migration failed: {e}")

if __name__ == "__main__":
    migrate()
