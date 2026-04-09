import os
from sqlalchemy import create_engine, Column, String, DateTime, Text, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Database URL from .env
# Example: postgresql://user:password@localhost:5432/jira_automation
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL")

if not SQLALCHEMY_DATABASE_URL:
    # Fallback to local sqlite if Postgres not configured yet
    SQLALCHEMY_DATABASE_URL = "sqlite:///./sql_app.db"

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class IncidentDB(Base):
    __tablename__ = "incidents"

    key = Column(String, primary_key=True, index=True)
    summary = Column(String)
    description = Column(Text)
    priority = Column(String)
    reporter_email = Column(String)
    status = Column(String)
    type = Column(String)
    action = Column(Text)
    confidence = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)

# Create tables
def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
