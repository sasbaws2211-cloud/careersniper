"""Command line for the admin app.

    python -m admin run                              start the admin web app (default 127.0.0.1:8030)
    python -m admin create-admin you@example.com     make an admin (add --owner for the first one); asks for the password
    python -m admin list-admins
    python -m admin set-password you@example.com
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User

from . import actions, config


def _password(args) -> str:
    env = os.getenv("ADMIN_NEW_PASSWORD")               # for scripts; a prompt is safer on a shared machine (nothing lands in shell history)
    if env:
        return env
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Again: "):
        raise SystemExit("The two passwords differ.")
    return first


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m admin", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--host", default=config.HOST)
    run.add_argument("--port", type=int, default=config.PORT)
    ca = sub.add_parser("create-admin")
    ca.add_argument("email")
    ca.add_argument("--owner", action="store_true", help="owners can also create and switch off admins and delete members")
    sub.add_parser("list-admins")
    sp = sub.add_parser("set-password")
    sp.add_argument("email")
    args = ap.parse_args(argv)

    if args.cmd == "run":
        import uvicorn
        bad = config.problems()
        if bad and config.IS_PROD:
            print("Refusing to start:\n  - " + "\n  - ".join(bad), file=sys.stderr)
            return 1
        uvicorn.run("admin.main:app", host=args.host, port=args.port, log_level="info")
        return 0
    return asyncio.run(_run(args))


async def _run(args) -> int:
    async with SessionLocal() as db:
        if args.cmd == "create-admin":
            try:
                u = await actions.create_admin(db, None, args.email, _password(args), "owner" if args.owner else "admin", ip="command-line", by="command-line")
            except actions.AdminError as e:
                print(f"Not created: {e}", file=sys.stderr)
                return 1
            print(f"Created {u.role} {u.email}. Start the admin app with: python -m admin run")
        elif args.cmd == "list-admins":
            for u in (await db.scalars(select(User).order_by(User.id))):
                print(f"{u.id:>4}  {u.role:<9} {u.email}  (created {u.created_at:%Y-%m-%d})")
        elif args.cmd == "set-password":
            u = await db.scalar(select(User).where(User.email == args.email.strip().lower()))
            if not u or u.role not in actions.ROLES:
                print("No such admin.", file=sys.stderr)
                return 1
            try:
                await actions.set_admin_password(db, actions.Admin(u.id, u.email, "owner"), u.id, _password(args), "command-line")
            except actions.AdminError as e:
                print(f"Not changed: {e}", file=sys.stderr)
                return 1
            print("Password changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
