# onboarding a new vendor

1. Get the vendor to confirm: hostname, username, key type, the exact filename
   pattern, and whether they write a sentinel. If they will not write a
   sentinel, say no. We have been burned by half-transferred files twice and
   both times it was a vendor without one.
2. Ask the platform team for a vault entry at `vendor/<name>/sftp` and a key
   pair. Never put a key in this repo — `.gitignore` covers `*.pem` and
   `secrets/` but that is a backstop, not a policy.
3. Add the vendor to `config/vendors.yml`. `enabled: false` until the first
   test delivery lands.
4. Add the feed to `FEEDS` in `loader/vendor_load.py` (yes, it is a list in
   the source; moving it into `vendors.yml` is on the list).
5. Add the raw table to `dbt_project/models/sources.yml` and a staging model.
6. Flip `enabled: true` and watch `./run.sh poll` for a week.

## request the firewall change first

The edge box egress rules are per-host. Lead time is about a fortnight and it
is always the thing that holds an onboarding up.

## the old process

Before the edge box there was a per-vendor cron on the analytics VM that used
password auth out of a file called `sftp.conf`. That VM is gone and so is the
file. If you find a `sftp.conf` anywhere, it is dead and the passwords in it
were rotated at decommission — delete it and tell security you found it.
