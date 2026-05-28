from sqlalchemy import Column, Integer, String, Boolean, Float, LargeBinary, ForeignKey, Table
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

# ──────────────────────────────────────────────
# Join table for Activity <-> Instructor many-to-many
# ──────────────────────────────────────────────
activity_instructors = Table(
    'activity_instructors',
    Base.metadata,
    Column('activity_id', String, ForeignKey('activities.activity_id', ondelete='CASCADE')),
    Column('instructor_id', Integer, ForeignKey('instructors.id', ondelete='CASCADE'))
)


# ──────────────────────────────────────────────
# activities
# ──────────────────────────────────────────────
class Activity(Base):
    __tablename__ = 'activities'

    activity_id   = Column(String, primary_key=True)
    activity_name = Column(String, nullable=False)
    enabled       = Column(Boolean, default=True)
    # Path to the directory containing per-task grader scripts (grade_task#.py)
    task_graders  = Column(String, nullable=True)
    # Section identifier (alphanumeric with special characters such as hyphens, e.g. "11637-B")
    section       = Column(String, nullable=True)
    # 4-digit academic year (e.g. 2024)
    year          = Column(Integer, nullable=True)
    # Semester string (e.g. "Fall", "Spring", "Summer")
    semester      = Column(String, nullable=True)
    instructors = relationship('Instructor', secondary=activity_instructors,
                               back_populates='activities')

    # New relationship to UserActivity
    user_activities = relationship('UserActivity', back_populates='activity',
                                   cascade='all, delete-orphan')


# ──────────────────────────────────────────────
# users
# ──────────────────────────────────────────────

class User(Base):
    __tablename__ = 'users'

    id       = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, nullable=True)
    name     = Column(String, nullable=False)
    email    = Column(String, unique=True, nullable=False)

    # One user → many activity enrollments
    activities = relationship('UserActivity', back_populates='user',
                              cascade='all, delete-orphan')


# ──────────────────────────────────────────────
# user_activities
# ──────────────────────────────────────────────

class UserActivity(Base):
    __tablename__ = 'user_activities'

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'),
                         nullable=False)
    activity_id = Column(String, ForeignKey('activities.activity_id', ondelete='CASCADE'),
                         nullable=False)
    password        = Column(String,  nullable=True)
    prequiz_token   = Column(String,  nullable=True)
    postquiz_token  = Column(String,  nullable=True)
    room_name       = Column(String,  nullable=True)
    # Role of the user in this activity: "Student", "Instructor", "TA", or "Admin"
    role            = Column(String,  nullable=True)

    user     = relationship('User',     back_populates='activities')
    activity = relationship('Activity', back_populates='user_activities')

    # One enrollment → many submissions
    submissions = relationship('Submission', back_populates='user_activity',
                               cascade='all, delete-orphan',
                               order_by='Submission.submitted_at.desc()')


# ──────────────────────────────────────────────
# submissions 
# ──────────────────────────────────────────────

class Submission(Base):
    __tablename__ = 'submissions'

    id               = Column(Integer, primary_key=True, autoincrement=True)
    user_activity_id = Column(Integer,
                              ForeignKey('user_activities.id', ondelete='CASCADE'),
                              nullable=False)
    notebook          = Column(LargeBinary, nullable=True)   # raw .ipynb bytes
    notebook_filename = Column(String, nullable=True)
    submitted_at      = Column(String, nullable=True)        # ISO-format timestamp string
    score             = Column(Float,  nullable=True)
    feedback          = Column(String, nullable=True)        # Text feedback document

    user_activity = relationship('UserActivity', back_populates='submissions')


# ──────────────────────────────────────────────
# instructors
# ──────────────────────────────────────────────

class Instructor(Base):
    __tablename__ = 'instructors'

    id    = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False)
    name  = Column(String, nullable=True)

    activities = relationship('Activity', secondary=activity_instructors,
                              back_populates='instructors')
