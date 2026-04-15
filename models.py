from sqlalchemy import Column, Integer, String, Boolean, Float, LargeBinary, ForeignKey, Table
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

# Join table for Activity <-> Instructor many-to-many
activity_instructors = Table(
    'activity_instructors',
    Base.metadata,
    Column('activity_id', String, ForeignKey('activities.activity_id', ondelete='CASCADE')),
    Column('instructor_id', Integer, ForeignKey('instructors.id', ondelete='CASCADE'))
)


class Activity(Base):
    __tablename__ = 'activities'

    activity_id   = Column(String, primary_key=True)
    activity_name = Column(String, nullable=False)
    enabled       = Column(Boolean, default=True)
    # Path to the directory containing per-task grader scripts (grade_task#.py)
    task_graders  = Column(String, nullable=True)

    users       = relationship('UserSubmission', back_populates='activity',
                               cascade='all, delete-orphan')
    instructors = relationship('Instructor', secondary=activity_instructors,
                               back_populates='activities')


class UserSubmission(Base):
    __tablename__ = 'user_submissions'

    id          = Column(Integer, primary_key=True, autoincrement=True)
    activity_id = Column(String, ForeignKey('activities.activity_id', ondelete='CASCADE'),
                         nullable=False)
    username    = Column(String, nullable=False)
    name        = Column(String, nullable=False)
    email       = Column(String, nullable=True)
    prequiz_token  = Column(String, nullable=True)
    postquiz_token = Column(String, nullable=True)

    activity  = relationship('Activity', back_populates='users')
    notebooks = relationship('Notebook', back_populates='user_submission',
                             cascade='all, delete-orphan',
                             order_by='Notebook.submitted_at.desc()')


class Notebook(Base):
    __tablename__ = 'notebooks'

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    user_submission_id = Column(Integer,
                                ForeignKey('user_submissions.id', ondelete='CASCADE'),
                                nullable=False)
    notebook           = Column(LargeBinary, nullable=True)  # raw .ipynb bytes
    notebook_filename  = Column(String, nullable=True)
    submitted_at       = Column(String, nullable=True)   # ISO-format timestamp string
    score              = Column(Float,  nullable=True)
    feedback           = Column(String, nullable=True)   # Text feedback document

    user_submission = relationship('UserSubmission', back_populates='notebooks')


class Instructor(Base):
    __tablename__ = 'instructors'

    id    = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False)
    name  = Column(String, nullable=True)

    activities = relationship('Activity', secondary=activity_instructors,
                              back_populates='instructors')
