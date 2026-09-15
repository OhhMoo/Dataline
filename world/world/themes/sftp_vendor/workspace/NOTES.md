# notes

no changelog, this is the changelog. newest at the top. add a line when you
change something that would surprise the next person at 06:00.

**2026-03-01** globex sent tuesday's returns twice. loader is
overwrite-by-filename so the second copy replaced the first and nothing
doubled. this is the only reason we have not been bitten by a redelivery.

**2026-02-14** dropped GLOBEX_SFTP_* from .envrc, they use the acme key now.
config/vendors.yml still says otherwise. neither is read by anything in this
repo so it has not mattered.

**2026-02-02** `--on-redelivery append` got turned on for one run during the
january backfill and the day doubled. turned it back. do not do this.

**2026-01-20** added assert_net_revenue_not_negative as a WARNING. it fires
roughly once a fortnight when acme is late and that is not a bug.

**2025-11-18** initech offboarded. folder + sweeper entry left in place, see
config/vendors.yml.

**2025-10-02** moved the profile into config/ so bare `dbt` stops picking up
the old shared ~/.dbt one. half the team still has the old one.
