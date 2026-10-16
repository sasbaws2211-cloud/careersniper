"""python -m app.cli migrate | funnel [days] | bootstrap | scrape | digests | mail-test [email] | add <ats> <token> <Name> | discover candidates <file> | discover crawl | verify | prune-dead | data-health | reclassify | doctor"""
import asyncio
import sys

from .db import SessionLocal, engine
from .models import Company
from .scrapers import scrape_all, scrape_company
from .seed import seed_registry


def _mask(v: str) -> str:
    return "(not set)" if not v else f"(set, {len(v)} characters)"   # never print any part of a secret


async def data_health(db) -> int:
    """Summarize registry, scrape errors, job freshness, and dedupe coverage."""
    from datetime import datetime, timedelta
    from sqlalchemy import func, select, or_
    from . import config
    from .models import Company, Job
    now = datetime.utcnow()
    companies = list((await db.scalars(select(Company).where(Company.ats != "direct"))))
    active_co = [c for c in companies if c.active]
    errored = [c for c in active_co if c.last_error]
    never = [c for c in active_co if not c.last_scraped]
    jobs_active = (await db.scalar(select(func.count()).select_from(Job).where(Job.active.is_(True)))) or 0
    jobs_total = (await db.scalar(select(func.count()).select_from(Job))) or 0
    stale_cut = now - timedelta(days=config.JOB_STALE_DAYS)
    age_cut = now - timedelta(days=config.DEFAULT_MAX_JOB_AGE_DAYS)
    jobs_stale = (await db.scalar(select(func.count()).select_from(Job).where(
        Job.active.is_(True), Job.last_seen < stale_cut))) or 0
    jobs_old = (await db.scalar(select(func.count()).select_from(Job).where(
        Job.active.is_(True),
        func.coalesce(Job.posted_at, Job.first_seen) < age_cut))) or 0
    no_dup = (await db.scalar(select(func.count()).select_from(Job).where(
        Job.active.is_(True), Job.dup_key.is_(None)))) or 0
    remote = (await db.scalar(select(func.count()).select_from(Job).where(
        Job.active.is_(True), Job.remote.is_(True)))) or 0

    print(f"Employers: {len(active_co)} active / {len(companies)} total")
    print(f"  never scraped: {len(never)}")
    print(f"  with last_error: {len(errored)}")
    if errored:
        for c in sorted(errored, key=lambda x: x.last_scraped or now)[:15]:
            print(f"    - {c.ats}:{c.board_token} ({c.name}): {(c.last_error or '')[:80]}")
        if len(errored) > 15:
            print(f"    … and {len(errored)-15} more")
    print(f"Jobs: {jobs_active} active / {jobs_total} total")
    print(f"  remote: {remote}")
    print(f"  would be stale (last_seen > {config.JOB_STALE_DAYS}d): {jobs_stale}")
    print(f"  older than max age ({config.DEFAULT_MAX_JOB_AGE_DAYS}d by posted/first_seen): {jobs_old}")
    print(f"  missing dup_key: {no_dup}")
    print(f"Config: SCRAPE_INTERVAL_HOURS={config.SCRAPE_INTERVAL_HOURS} JOB_STALE_DAYS={config.JOB_STALE_DAYS} "
          f"DEFAULT_MAX_JOB_AGE_DAYS={config.DEFAULT_MAX_JOB_AGE_DAYS} CLOSE_GRACE_HOURS={config.CLOSE_GRACE_HOURS}")
    return 0 if jobs_active > 0 or len(active_co) > 0 else 1


async def prune_dead_employers(db, min_failures: int = 3) -> int:
    """Deactivate boards that hard-404 or fail repeatedly with zero active jobs."""
    from sqlalchemy import select, func
    from .models import Company, Job
    n = 0
    for c in (await db.scalars(select(Company).where(Company.active.is_(True), Company.ats != "direct"))):
        if not c.last_error or not c.last_scraped:
            continue
        active_jobs = (await db.scalar(select(func.count()).select_from(Job).where(
            Job.company_id == c.id, Job.active.is_(True)))) or 0
        err = (c.last_error or "").lower()
        hard = any(x in err for x in ("404", "not found", "unknown board", "invalid", "no such"))
        fails = 0
        if err.startswith("fail:"):
            try:
                fails = int(err.split(":", 2)[1])
            except (IndexError, ValueError):
                fails = 0
        if active_jobs == 0 and (hard or fails >= min_failures):
            c.active = False
            n += 1
    await db.commit()
    return n


async def bootstrap_data(db) -> dict:
    """Production data bootstrap: seed registry → full scrape → prune dead → health report."""
    from .seed import seed_registry
    from .scrapers import scrape_all
    added = await seed_registry(db)
    print(f"seed: +{added} employers", flush=True)
    stats = await scrape_all(db, only_stale_hours=None)  # full scrape, ignore interval
    print(f"scrape: {stats}", flush=True)
    pruned = await prune_dead_employers(db)
    print(f"prune: deactivated {pruned}", flush=True)
    await data_health(db)
    return {"seeded": added, "scrape": stats, "pruned": pruned}


async def doctor(db) -> int:
    """Pre-flight check for a deployment: configuration, database, schema, search index, email, AI. Exit code 1 on any failure."""
    from sqlalchemy import text
    from . import config, schema
    print(f"Environment: {config.APP_ENV}   Database: {engine.dialect.name}\n")
    fatal, warn = await config.production_problems(force=True)
    failed = bool(fatal) and config.IS_PROD
    for f in fatal:
        print(("FAIL  " if config.IS_PROD else "NOTE  ") + f)
    for w in warn:
        print("WARN  " + w)
    if not fatal and not warn:
        print("PASS  configuration looks production-ready")
    print()
    for label, val in (("SECRET_KEY", config.SECRET_KEY), ("PAYSTACK_SECRET_KEY", getattr(config, "PAYSTACK_SECRET_KEY", "")), ("PAYSTACK_PUBLIC_KEY", getattr(config, "PAYSTACK_PUBLIC_KEY", "")),
                       ("PAYSTACK_PUBLIC_KEY", getattr(config, "PAYSTACK_PUBLIC_KEY", "")), ("RESEND_API_KEY", getattr(config, "RESEND_API_KEY", "")), ("INTERNAL_TOKEN", config.INTERNAL_TOKEN)):
        print(f"      {label:18} {_mask(val)}")
    print(f"      BASE_DOMAIN        {config.BASE_DOMAIN}\n      PUBLIC_URL         {config.PUBLIC_URL}\n      MAIL_FROM          {config.MAIL_FROM}  (Resend: {'yes' if getattr(config, 'RESEND_API_KEY', '') else 'no'})\n")
    try:
        await db.execute(text("SELECT 1"))
        print("PASS  database reachable")
    except Exception as e:
        print(f"FAIL  database unreachable: {type(e).__name__}")
        return 1
    cur, head = schema.current_revision(), schema.head_revision()
    if head is None:
        print("WARN  alembic is not installed or configured; cannot check the schema version")
    elif cur == head:
        print(f"PASS  database schema is up to date ({cur})")
    else:
        print(f"WARN  database schema is at {cur or 'nothing (new database)'} but the code expects {head}. "
              "The container applies this automatically at start; otherwise run `alembic upgrade head`.")
    if engine.dialect.name == "postgresql":
        has = (await db.execute(text("SELECT count(*) FROM information_schema.columns WHERE table_name='jobs' AND column_name='search_vector'"))).scalar()
        print("PASS  full-text search index present" if has else "WARN  full-text search column missing (created at first start)")
    return 1 if failed else 0


def main(argv):
    asyncio.run(_main(argv))


async def _main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # company names are not always ASCII
    if argv[:1] == ["migrate"] and len(argv) == 3:
        from .migrate import migrate
        print(migrate(argv[1], argv[2]))
        return
    async with SessionLocal() as db:   # the schema comes from `alembic upgrade head`
        await seed_registry(db)
        if argv[:1] == ["scrape"]:
            print((await scrape_all(db)))
        elif argv[:1] == ["bootstrap"]:
            print((await bootstrap_data(db)))
        elif argv[:1] == ["digests"]:
            from .alerts import run_digests
            print((await run_digests(db)))
        elif argv[:2] == ["discover", "candidates"] and len(argv) >= 3:
            from . import discover
            names = [l.strip() for l in open(argv[2], encoding="utf-8") if l.strip() and not l.startswith("#")]
            hits = discover.probe_candidates(names)
            print(f"{len(hits)} of {len(names)} names matched a live board")
            print((await discover.register(db, hits, log=lambda m: print(m, flush=True))))
        elif argv[:1] == ["data-health"]:
            raise SystemExit((await data_health(db)))
        elif argv[:1] == ["prune-dead"]:
            n = await prune_dead_employers(db)
            print(f"deactivated {n} dead employers")
        elif argv[:1] == ["doctor"]:
            raise SystemExit((await doctor(db)))
        elif argv[:1] == ["ai-check"]:
            from . import ai
            try:
                print((await ai.check()))
            except ai.AIError as e:
                print(f"FAIL  {e}")
                raise SystemExit(1)
        elif argv[:1] == ["loomail-check"]:
            from . import loomail
            out = await loomail.check()
            print(out)
            raise SystemExit(0 if out.startswith("OK") else 1)
        elif argv[:1] == ["funnel"]:
            from . import funnel
            print(funnel.render((await funnel.report(db, int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 30))))
        elif argv[:1] == ["ai-usage"]:
            from . import ai
            rows = await ai.usage_report(db)
            print(f"{'tool':22} {'calls':>6} {'avg in':>8} {'avg out':>8} {'total in':>10} {'total out':>10}")
            for r in rows:
                print(f"{r['tool']:22} {r['calls']:>6} {r['avg_in']:>8} {r['avg_out']:>8} {r['total_in']:>10} {r['total_out']:>10}")
            if not rows:
                print("No AI calls recorded yet.")
        elif argv[:1] == ["refund"] and len(argv) == 2:
            from . import billing, referrals
            email = argv[1].strip().lower()
            ended = await billing.refund_member(db, email)
            reward = await referrals.withdraw(db, email)
            print(f"{email}: ended {ended} paid plan(s); referral reward {reward}.")
            print("Now return the money in the Paystack dashboard (Transactions > the payment > Refund) and, for a recurring plan, disable the subscription there too. This command does not move any money.")
        elif argv[:1] == ["verify"]:
            from . import discover
            await discover.verify_boards(db, log=lambda m: print(m, flush=True))
        elif argv[:2] == ["discover", "crawl"]:
            from . import discover
            print((await discover.discover_from_crawl(db, log=lambda m: print(m, flush=True))))
        elif argv[:1] == ["reclassify"]:
            from . import filters
            print((await filters.backfill(db, everything=True)), "jobs reclassified")
        elif argv[:1] == ["add"] and len(argv) >= 4:
            from .scrapers import clean_token
            c = Company(ats=argv[1], board_token=clean_token(argv[1], argv[2]), name=" ".join(argv[3:]))
            db.add(c)
            await db.commit()
            print((await scrape_company(db, c)), "jobs", c.last_error)
        else:
            print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
