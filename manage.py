#!/usr/bin/env python3
"""
Command-line management utility for activity_server.

Run this inside the running app container, e.g.:

    docker compose exec app python manage.py list-activities
    docker compose exec app python manage.py change-instructor <activity_id> <instructor_email>
    docker compose exec app python manage.py change-instructor <activity_id> <instructor_email> --name "Jane Doe"
    docker compose exec app python manage.py add-instructor <instructor_email>
    docker compose exec app python manage.py add-instructor <instructor_email> --name "Jane Doe"
    docker compose exec app python manage.py list-instructors
    docker compose exec app python manage.py set-admin-password
    docker compose exec app python manage.py set-admin-password "some-password"

It talks to Postgres directly via SQLAlchemy (using the same DATABASE_URL
the app itself uses), so it works whether or not the API server considers
you an authenticated instructor.
"""

import argparse
import getpass
import os
import sys

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from auth_utils import hash_password
from models import Activity, Base, Instructor

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://activity_user:activity_pass@db:5432/activity_db",
)


def _get_session():
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(bind=engine)
    # Same idempotent, additive patch main.py applies at startup — keeps
    # this script usable even if it's ever run before the app container.
    existing_cols = {c["name"] for c in inspect(engine).get_columns("instructors")}
    with engine.begin() as conn:
        if "password_hash" not in existing_cols:
            conn.execute(text(
                "ALTER TABLE instructors ADD COLUMN password_hash VARCHAR"
            ))
        if "is_admin" not in existing_cols:
            conn.execute(text(
                "ALTER TABLE instructors ADD COLUMN is_admin "
                "BOOLEAN NOT NULL DEFAULT FALSE"
            ))
    Session = sessionmaker(bind=engine)
    return Session()


def cmd_change_instructor(args):
    """Reassign the instructor for an activity, replacing any current one(s)."""
    db = _get_session()
    try:
        activity = db.query(Activity).filter(
            Activity.activity_id == args.activity_id
        ).first()
        if not activity:
            print(f"Error: activity '{args.activity_id}' not found.", file=sys.stderr)
            sys.exit(1)

        instructor = db.query(Instructor).filter(
            Instructor.email == args.instructor_email
        ).first()
        if not instructor:
            instructor = Instructor(email=args.instructor_email, name=args.name)
            db.add(instructor)
            db.flush()
            print(f"Created new instructor record for {args.instructor_email}.")
        elif args.name:
            instructor.name = args.name

        old_instructors = [i.email for i in activity.instructors]
        activity.instructors = [instructor]
        db.commit()

        print(f"Activity '{activity.activity_id}' ({activity.activity_name}):")
        print(f"  previous instructor(s): {', '.join(old_instructors) or '(none)'}")
        print(f"  new instructor:         {instructor.email}")
    finally:
        db.close()


def cmd_add_instructor(args):
    """
    Add a "bare" instructor with no activity — they aren't a course
    instructor of anything yet, but once added they can sign in at
    /dashboard with Google (using this email) and create their own
    activities from there.
    """
    db = _get_session()
    try:
        instructor = db.query(Instructor).filter(
            Instructor.email == args.email
        ).first()
        if instructor:
            if args.name:
                instructor.name = args.name
                db.commit()
                print(f"Instructor {args.email} already existed; updated name to '{args.name}'.")
            else:
                print(f"Instructor {args.email} already exists (no changes made).")
            return

        instructor = Instructor(email=args.email, name=args.name)
        db.add(instructor)
        db.commit()
        print(f"Created instructor {args.email}.")
        print("They can now sign in at /dashboard with Google using this email "
              "and create their own activities.")
    finally:
        db.close()


def cmd_list_activities(args):
    db = _get_session()
    try:
        activities = db.query(Activity).order_by(Activity.activity_name).all()
        if not activities:
            print("No activities found.")
            return
        for a in activities:
            instr = ", ".join(i.email for i in a.instructors) or "(none)"
            status = "enabled" if a.enabled else "disabled"
            print(f"{a.activity_id:45s} [{status:8s}] instructors: {instr}")
    finally:
        db.close()


def cmd_list_instructors(args):
    db = _get_session()
    try:
        instructors = db.query(Instructor).order_by(Instructor.email).all()
        if not instructors:
            print("No instructors found.")
            return
        for i in instructors:
            role = "ADMIN" if i.is_admin else "instructor"
            has_pw = "yes" if i.password_hash else "no"
            n_activities = len(i.activities)
            print(
                f"{i.email:35s} [{role:10s}] name={(i.name or ''):20s} "
                f"activities={n_activities:<3d} password_set={has_pw}"
            )
    finally:
        db.close()


def cmd_set_admin_password(args):
    """Create the admin account if needed, and set/replace its password."""
    db = _get_session()
    try:
        password = args.password
        if not password:
            password = getpass.getpass("New admin password: ")
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                print("Error: passwords do not match.", file=sys.stderr)
                sys.exit(1)
        if not password:
            print("Error: password cannot be empty.", file=sys.stderr)
            sys.exit(1)

        admin = db.query(Instructor).filter(Instructor.is_admin.is_(True)).first()
        if not admin:
            admin = Instructor(email="admin", name="Administrator", is_admin=True)
            db.add(admin)
            db.flush()
            print("Created the admin account (email='admin').")

        admin.password_hash = hash_password(password)
        db.commit()
        print("Admin password set successfully. Sign in at /admin/login.")
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="activity_server management CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser(
        "change-instructor",
        help="Reassign the instructor for an activity (replaces existing assignment)",
    )
    p1.add_argument("activity_id")
    p1.add_argument("instructor_email")
    p1.add_argument("--name", default=None, help="Instructor display name (optional)")
    p1.set_defaults(func=cmd_change_instructor)

    p1b = sub.add_parser(
        "add-instructor",
        help="Add an instructor with no activity, so they can sign in and create their own",
    )
    p1b.add_argument("email")
    p1b.add_argument("--name", default=None, help="Instructor display name (optional)")
    p1b.set_defaults(func=cmd_add_instructor)

    p2 = sub.add_parser("list-activities", help="List all activities and their instructors")
    p2.set_defaults(func=cmd_list_activities)

    p3 = sub.add_parser("list-instructors", help="List all instructor accounts")
    p3.set_defaults(func=cmd_list_instructors)

    p4 = sub.add_parser(
        "set-admin-password",
        help="Create (if needed) and set the password for the admin account",
    )
    p4.add_argument(
        "password", nargs="?", default=None,
        help="New password (omit to be prompted securely)",
    )
    p4.set_defaults(func=cmd_set_admin_password)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()