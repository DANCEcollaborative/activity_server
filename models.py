"""
Database models for the activity server
Install: pip install sqlalchemy psycopg2-binary
"""
from sqlalchemy import (
    create_engine, Column, String, Integer, ForeignKey,
    LargeBinary, Float, Table, Boolean
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker
from datetime import datetime
import os

Base = declarative_base()

# Association table for activity instructors
activity_instructors = Table(
    'activity_instructors',
    Base.metadata,
    Column('activity_id', String, ForeignKey('activities.activity_id')),
    Column('instructor_id', Integer, ForeignKey('instructors.id'))
)

class Activity(Base):
    __tablename__ = 'activities'
    activity_id = Column(String, primary_key=True)
    activity_name = Column(String, nullable=False)  # Display name for activity
    enabled = Column(Boolean, default=True)         # Enable/disable activity
    grading_notebook = Column(LargeBinary)          # Store .ipynb file content
    grading_notebook_filename = Column(String)

    # Relationships
    users = relationship("UserSubmission", back_populates="activity", cascade="all, delete-orphan")
    instructors = relationship("Instructor", secondary=activity_instructors, back_populates="activities")

class UserSubmission(Base):
    __tablename__ = 'user_submissions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    activity_id = Column(String, ForeignKey('activities.activity_id'))
    username = Column(String, nullable=False)
    name = Column(String, nullable=False)
    email = Column(String, nullable=True)  # User email for Google OAuth
    prequiz_token = Column(String, nullable=True)  # Pre-quiz token
    postquiz_token = Column(String, nullable=True)  # Post-quiz token
    
    # Relationships
    activity = relationship("Activity", back_populates="users")
    notebooks = relationship(
        "Notebook",
        back_populates="submission",
        cascade="all, delete-orphan",
        order_by="Notebook.id"
    )

    __table_args__ = (
        {'sqlite_autoincrement': True},
    )

class Notebook(Base):
    __tablename__ = 'notebooks'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_submission_id = Column(Integer, ForeignKey('user_submissions.id', ondelete="CASCADE"), nullable=False)

    notebook = Column(LargeBinary, nullable=True)
    notebook_filename = Column(String, nullable=True)
    submitted_at = Column(String, nullable=True)  # Consider TIMESTAMPTZ later
    score = Column(Float, nullable=True)          # <-- score is per notebook attempt

    submission = relationship("UserSubmission", back_populates="notebooks")

class Instructor(Base):
    __tablename__ = 'instructors'
    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False)  # Google email for OAuth
    name = Column(String, nullable=True)                 # Display name from Google

    activities = relationship("Activity", secondary=activity_instructors, back_populates="instructors")

# Database connection and session management
class Database:
    def __init__(self, db_url=None):
        if db_url is None:
            # Default to PostgreSQL
            db_url = os.getenv(
                'DATABASE_URL',
                'postgresql://activity_user:activity_pass@localhost/activity_db'
            )
        self.engine = create_engine(db_url)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def create_tables(self):
        Base.metadata.create_all(bind=self.engine)

    def get_session(self):
        return self.SessionLocal()

# Initialize database
db = Database()