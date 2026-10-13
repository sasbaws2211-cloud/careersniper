"""CareerSniper platform admin: a separate web app (its own process, port, login and session) for running the platform.

It shares the database with the public site and uses the site's own code for anything that must behave identically (refunds,
referral withdrawal, deleting a member, scraping a company). Run it with `python -m admin run`; see admin/README.md.
"""
