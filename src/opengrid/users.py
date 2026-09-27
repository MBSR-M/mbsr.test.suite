"""Local operator provisioning: ``python -m opengrid.users create --help``."""

import argparse
import getpass
import os
import sys

from opengrid.db import session_factory
from opengrid.services import Conflict
from opengrid.ui_auth import ROLES, create_user


def main():
    parser = argparse.ArgumentParser(description="Provision a local OpenGrid operator account.")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Create a local user without replacing existing users")
    create.add_argument("--username", required=True)
    create.add_argument("--role", choices=ROLES, default="VIEWER")
    create.add_argument("--display-name")
    args = parser.parse_args()
    password = os.environ.get("UI_NEW_USER_PASSWORD")
    if password is None:
        if not sys.stdin.isatty():
            parser.error("use an interactive terminal or supply UI_NEW_USER_PASSWORD")
        password = getpass.getpass("New password (at least 12 characters): ")
        if password != getpass.getpass("Confirm new password: "):
            parser.error("passwords do not match")
    factory = session_factory()
    try:
        with factory.begin() as session:
            user = create_user(session, args.username, password, args.role, args.display_name)
            username, role = user.username, user.role
    except (ValueError, Conflict) as error:
        parser.error(str(error))
    finally:
        factory.kw["bind"].dispose()
    print(f"Created local user {username} with role {role}.")


if __name__ == "__main__":
    main()
