"""commerce_analytics theme hooks: a shop's nightly extracts, the raw load, and
the BI refresh.

The world is a mid-size e-commerce company (ergonomic office furniture, sold to
consumers, small businesses and enterprise accounts) whose analytics team runs
a dbt star schema under Power BI / Tableau / Sigma dashboards. This module is
the shop's operational system as the data team sees it: one change-data-capture
extract per business day, four entities, landed as CSV.

    data/landing/orders/dt=YYYY-MM-DD/orders.csv            new orders + status changes
    data/landing/order_lines/dt=YYYY-MM-DD/order_lines.csv  lines of that day's new orders
    data/landing/customers/dt=YYYY-MM-DD/customers.csv      signups + account changes
    data/landing/products/dt=YYYY-MM-DD/products.csv        launches, price changes, retirements

Why CDC, and why it matters for the BI tasks this world exists for: an order
placed on 20 January and refunded on 9 February is in TWO partitions - once as
`completed` (dt=2026-01-20) and once as `refunded` (dt=2026-02-09). Staging keeps
the latest version, so January's net revenue moves after January has been
closed. That is the single most common "the dashboard does not match Finance"
ticket in real analytics teams, and here it is a property of the data rather
than a planted defect.

## The calendar

day_index 0 is the CUTOVER snapshot (dt = vtime_start - 1 day): every customer
account and every catalogue item that existed when the shop moved to this
platform, and no orders - order history before cutover stayed in the old
system. day_index i >= 1 lands at vtime_start + i days, 02:00 UTC, and carries
business date vtime_start + i - 1. So with vtime_start 2025-01-01 and days 425:

    dt=2024-12-31 (cutover) .. dt=2026-02-28, world clock 2026-03-02T00:00Z

and every seeded job ran BEFORE the clock (the pipeline for business date D
runs at D+1 02:30), so `advance(24)` from a fresh build lands one extract and
executes one full nightly cycle. theme.yaml's schedule says the same thing.

## Determinism

- One PRNG per (seed, day_index), `Random(f"{seed}:{day_index}")`, as every
  theme. A day's draws come in two phases: phase A (how many signups, orders
  and lines) and phase B (everything else). Sequential ids - customer, order,
  line - are prefix sums of phase A over earlier days, which are pure functions
  of the seed, so day N's bytes never depend on the path to day N.
- Per-purpose streams for things that are not days: `f"{seed}:customer:{id}"`
  (an account's fixed attributes) - the same per-purpose rule files.py follows.
- The catalogue (names, list prices, launches, price changes) is static data
  below, positioned by day_index, so it moves with vtime_start.
- Money is integer cents end to end, rendered `%d.%02d` - never a float.
- No wall clock. Every timestamp is the business date plus seconds drawn from
  the day's PRNG.

A day's content is memoised per (params, day_index) because write_day(N) must
look back MAX_LAG days to emit the status changes that fall on N; a fresh
process (the live arrival stream) recomputes the look-back, a long-running one
(write_landing) computes each day once.
"""
import bisect
import csv
import hashlib
import json
import random
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from world import scheduler, workflow
from world.scheduler import parse_vtime

ENTITIES = ("orders", "order_lines", "customers", "products")
FIELDS = {
    "orders": ["order_id", "customer_id", "ordered_at", "status", "subtotal",
               "shipping", "currency", "updated_at"],
    "order_lines": ["order_line_id", "order_id", "product_id", "quantity",
                    "unit_price", "line_amount", "created_at"],
    "customers": ["customer_id", "customer_name", "email", "segment", "region",
                  "country", "signup_date", "updated_at"],
    "products": ["product_id", "sku", "product_name", "category", "unit_price",
                 "is_active", "updated_at"],
}

# Defaults for every scale knob, so a seed file names only what it changes and
# a key the seed leaves out is not a KeyError in the live arrival stream (which
# only carries the keys theme.yaml's live.params lists AND the seed set).
DEFAULTS = {
    "customers": 220,            # accounts on the platform at cutover
    "signup_pct": 18,            # chance per day of a new account (a second at a quarter of it)
    "update_pct": 9,             # chance per day that one account changes segment
    "orders_per_day": [20, 60],  # base range, before weekday / month / growth scaling
    "lines_per_order": [1, 4],
    "cancel_pct": 6,
    "refund_pct": 4,
    "growth_pct": 20,            # year-over-year order volume growth
}

FIRST_CUSTOMER_ID = 1001
FIRST_ORDER_ID = 100001
FIRST_LINE_ID = 1000001
# Refunds land up to 45 days after the order; next-day cancellations one day
# after. write_day(N) therefore looks back this many days for changes due on N.
REFUND_LAGS = (3, 3, 4, 5, 5, 6, 7, 7, 8, 9, 10, 11, 12, 14, 16, 18, 21, 25,
               30, 35, 45)
MAX_LAG = max(REFUND_LAGS) + 1

# --------------------------------------------------------------------------
# the catalogue: static data, positioned by day_index
# --------------------------------------------------------------------------

# (product_id, sku, name, category, list price in cents, popularity, kind)
CATALOG = (
    (101, "DSK-RDG-48", "Ridge Standing Desk 48in", "Desks", 54900, 9, "furniture"),
    (102, "DSK-RDG-60", "Ridge Standing Desk 60in", "Desks", 64900, 7, "furniture"),
    (103, "DSK-ATL-CRN", "Atlas Corner Desk", "Desks", 89900, 3, "furniture"),
    (104, "DSK-CMP-WRT", "Compact Writing Desk", "Desks", 22900, 6, "furniture"),
    (105, "DSK-CNV-PRO", "Desk Converter Pro", "Desks", 27900, 5, "furniture"),
    (106, "CHR-CTR-STD", "Contour Task Chair", "Seating", 38900, 10, "furniture"),
    (107, "CHR-CTR-MSH", "Contour Task Chair Mesh", "Seating", 42900, 8, "furniture"),
    (108, "CHR-EXC-LNG", "Executive Lounge Chair", "Seating", 114900, 2, "furniture"),
    (109, "CHR-BAL-STL", "Balance Stool", "Seating", 14900, 5, "furniture"),
    (110, "CHR-DRF-STD", "Drafting Chair", "Seating", 31900, 3, "furniture"),
    (111, "ACC-ARM-SGL", "Monitor Arm Single", "Accessories", 12900, 12, "accessory"),
    (112, "ACC-ARM-DUO", "Monitor Arm Dual", "Accessories", 19900, 9, "accessory"),
    (113, "ACC-CBL-TRY", "Cable Tray", "Accessories", 3900, 14, "accessory"),
    (114, "ACC-MAT-AFG", "Anti-Fatigue Mat", "Accessories", 7900, 11, "accessory"),
    (115, "ACC-LAP-STD", "Laptop Stand", "Accessories", 5900, 13, "accessory"),
    (116, "ACC-FTR-ADJ", "Adjustable Footrest", "Accessories", 4500, 8, "accessory"),
    (117, "ACC-DSK-ORG", "Desk Organizer", "Accessories", 2900, 10, "accessory"),
    (118, "LGT-HLO-DSK", "Halo Desk Lamp", "Lighting", 8900, 9, "accessory"),
    (119, "LGT-HLO-FLR", "Halo Floor Lamp", "Lighting", 15900, 4, "accessory"),
    (120, "LGT-BIA-STR", "Bias Light Strip", "Lighting", 3500, 7, "accessory"),
    (121, "SVC-ASM-WHG", "White-Glove Assembly", "Services", 14900, 5, "service"),
    (122, "SVC-ASM-STD", "Standard Assembly", "Services", 7900, 6, "service"),
    (123, "SVC-WRN-3YR", "Extended Warranty 3yr", "Services", 9900, 7, "service"),
    (124, "SVC-DSN-CNS", "Workspace Design Consult", "Services", 45000, 2, "service"),
    (125, "ACC-ACS-PNL", "Acoustic Desk Panel", "Accessories", 11900, 6, "accessory"),
)
# Not every product is in the cutover catalogue: day_index it launches on.
LAUNCHES = {105: 130, 125: 250, 119: 330}
# (day_index, product_id, new list price in cents). The July price list and the
# January one: why `List Price Revenue` (quantity x today's price) and the
# revenue actually booked (quantity x the price on the day) disagree.
PRICE_CHANGES = ((182, 101, 57900), (182, 102, 67900), (182, 107, 44900),
                 (366, 112, 21900), (366, 121, 16900))
DISCONTINUED = {110: 300}            # product_id -> day_index it is retired on

# Who buys what: weight multiplier by (segment, product kind).
AFFINITY = {
    "Consumer": {"furniture": 2, "accessory": 4, "service": 1},
    "SMB": {"furniture": 4, "accessory": 3, "service": 2},
    "Enterprise": {"furniture": 5, "accessory": 2, "service": 3},
}
# Shipping in cents by region; consumer parcels vs business freight.
SHIP_CONSUMER = {"NA": 995, "EMEA": 1995, "APAC": 2495}
SHIP_BUSINESS = {"NA": 4900, "EMEA": 8900, "APAC": 11900}
FREE_SHIP_CONSUMER = 50000           # $500.00
FREE_SHIP_BUSINESS = 250000          # $2,500.00

WEEKDAY_PCT = (100, 100, 100, 100, 100, 75, 65)                   # Mon..Sun
MONTH_PCT = (88, 90, 98, 100, 100, 97, 92, 95, 105, 108, 125, 118)  # Jan..Dec
PEAK_PCT = 180                                                    # Black Friday, Cyber Monday

# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------

SEGMENTS = (("Enterprise", 15), ("SMB", 35), ("Consumer", 50))
REGIONS = (("NA", 50), ("EMEA", 30), ("APAC", 20))
COUNTRIES = {
    "NA": (("US", 72), ("CA", 22), ("MX", 6)),
    "EMEA": (("GB", 30), ("DE", 25), ("FR", 14), ("NL", 12), ("IE", 7), ("SE", 7),
             ("ES", 5)),
    "APAC": (("AU", 40), ("JP", 20), ("SG", 18), ("NZ", 12), ("IN", 10)),
}
FIRST_NAMES = ("Ava", "Ben", "Chloe", "Daniel", "Elena", "Farah", "Gabriel", "Hana",
               "Isaac", "Jade", "Kofi", "Lena", "Marco", "Nadia", "Owen", "Priya",
               "Quinn", "Rosa", "Samir", "Tara", "Umar", "Vera", "Wes", "Yara",
               "Zoe", "Arjun", "Bianca", "Callum", "Dina", "Emil", "Freya", "Hugo",
               "Ines", "Jonas", "Kira", "Luca", "Maya", "Nils", "Olga", "Pavel")
LAST_NAMES = ("Abbott", "Barros", "Castillo", "Dunn", "Eriksen", "Fontaine", "Garcia",
              "Hughes", "Iqbal", "Jensen", "Kaur", "Lindqvist", "Mendes", "Novak",
              "Osei", "Park", "Quintero", "Rossi", "Sato", "Tanaka", "Ueda", "Vargas",
              "Walsh", "Xu", "Yilmaz", "Zielinski", "Brennan", "Clarke", "Duarte",
              "Ferreira", "Haddad", "Ito", "Kowalczyk", "Moreau", "Nakamura", "Petrov")
PLACES = ("Northgate", "Bluefin", "Harbor", "Summit", "Cedar", "Ironwood", "Lakeside",
          "Redstone", "Westmere", "Brightline", "Copperleaf", "Driftwood", "Elmstead",
          "Foxglove", "Granite", "Highfield", "Kestrel", "Larchmont", "Meridian",
          "Oakhurst", "Pinecrest", "Quarry", "Riverbend", "Silverline", "Tidewater",
          "Upland", "Valemont", "Willowby", "Ashcombe", "Beacon")
SMB_TRADES = ("Design Studio", "Dental Group", "Architects", "Accounting", "Legal",
              "Coworking", "Marketing", "Physio", "Engineering", "Consulting",
              "Realty", "Print Works", "Media", "Veterinary", "Tutoring")
SMB_SUFFIX = ("LLC", "Ltd", "Co.", "Partners", "& Sons")
ENT_INDUSTRY = ("Logistics", "Health Systems", "Financial", "Insurance", "Energy",
                "Telecom", "Software", "Manufacturing", "Pharma", "Retail Group",
                "Bank", "Freight", "Media", "Hospitality", "Analytics")
ENT_SUFFIX = ("Inc.", "Group", "plc", "Holdings", "Corporation", "GmbH", "AG")


def _weighted(rng: random.Random, table):
    """Integer-weighted pick: one randrange, a linear walk. No float cumulative
    sums, so the pick cannot depend on how a host rounds them."""
    total = sum(w for _, w in table)
    pick = rng.randrange(total)
    for value, weight in table:
        if pick < weight:
            return value
        pick -= weight
    raise AssertionError("unreachable")


def _slug(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def customer_profile(seed: int, customer_id: int, cutover: date, initial: int) -> dict:
    """An account's fixed attributes: a pure function of (seed, customer_id).

    Accounts that existed at cutover (the first `initial` ids) signed up some
    time in the 1000 days before it; later accounts carry the business date
    they signed up on, which the caller supplies. Only `segment` ever changes
    afterwards (an upgrade), and an update row re-sends every other field
    unchanged - which is what lets an update be emitted without replaying the
    account's history.
    """
    rng = random.Random(f"{seed}:customer:{customer_id}")
    segment = _weighted(rng, SEGMENTS)
    region = _weighted(rng, REGIONS)
    country = _weighted(rng, COUNTRIES[region])
    if segment == "Consumer":
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        name = f"{first} {last}"
        email = f"{first.lower()}.{last.lower()}{rng.randrange(10, 99)}@example.com"
        activity = 1
    elif segment == "SMB":
        name = f"{rng.choice(PLACES)} {rng.choice(SMB_TRADES)} {rng.choice(SMB_SUFFIX)}"
        email = f"office@{_slug(name)[:24]}.example"
        activity = 2 + rng.randrange(3)
    else:
        name = f"{rng.choice(PLACES)} {rng.choice(ENT_INDUSTRY)} {rng.choice(ENT_SUFFIX)}"
        email = f"procurement@{_slug(name)[:24]}.example"
        activity = 5 + rng.randrange(4)
    signup = None
    if customer_id < FIRST_CUSTOMER_ID + initial:
        signup = cutover - timedelta(days=1 + rng.randrange(1000))
    return {"customer_id": customer_id, "customer_name": name, "email": email,
            "segment": segment, "region": region, "country": country,
            "signup_date": signup, "activity": activity}


UPGRADE = {"Consumer": "SMB", "SMB": "Enterprise", "Enterprise": "Enterprise"}

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def money(cents: int) -> str:
    """Integer cents -> '1234.50'. Never through a float."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def _stamp(day: date, seconds: int) -> str:
    moment = datetime(day.year, day.month, day.day) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _peak_days(year: int) -> tuple[date, date]:
    """Black Friday and Cyber Monday: the Friday after the fourth Thursday of
    November and the Monday after it."""
    first = date(year, 11, 1)
    thursday = first + timedelta(days=(3 - first.weekday()) % 7 + 21)
    return thursday + timedelta(days=1), thursday + timedelta(days=4)


def params_of(seed: dict) -> dict:
    """The seed's scale knobs over DEFAULTS. `seed` is the build seed or the
    live-world seed (`scheduler.live_seed`) - both carry `seed` and
    `vtime_start`; the knobs are optional in either."""
    out = {k: seed.get(k, v) for k, v in DEFAULTS.items()}
    out["seed"] = seed["seed"]
    out["vtime_start"] = seed["vtime_start"]
    return out


def _key(params: dict) -> str:
    return json.dumps(params, sort_keys=True)


def cutover(params: dict) -> date:
    """Business date of day_index 0: the day before vtime_start."""
    return parse_vtime(params["vtime_start"]).date() - timedelta(days=1)


def business_date(params: dict, day_index: int) -> date:
    return cutover(params) + timedelta(days=day_index)


# --------------------------------------------------------------------------
# the day generator
# --------------------------------------------------------------------------

class _Shop:
    """Memoised generator state for one parameter set.

    Holds nothing that is not a pure function of (params, day_index): the
    per-day plans and contents, the prefix sums that turn them into ids, and
    each account's fixed profile. Dropping the cache changes no byte.
    """

    def __init__(self, params: dict):
        self.p = params
        self.seed = params["seed"]
        self.cut = cutover(params)
        self.initial = int(params["customers"])
        self.plans: dict[int, dict] = {}
        self.contents: dict[int, dict] = {}
        self.bases = [(FIRST_CUSTOMER_ID + self.initial, FIRST_ORDER_ID, FIRST_LINE_ID)]
        self.profiles: dict[int, dict] = {}
        self.cum: list[int] = []          # cumulative activity weights, by id order

    # ---- phase A: counts only -------------------------------------------
    def _phase_a(self, day_index: int) -> tuple[random.Random, dict]:
        rng = random.Random(f"{self.seed}:{day_index}")
        if day_index == 0:
            # The cutover snapshot: accounts and catalogue, no orders.
            return rng, {"signups": 0, "update": False, "lines": []}
        pct = int(self.p["signup_pct"])
        r = rng.randrange(400)
        signups = (1 if r < pct * 4 else 0) + (1 if r < pct else 0)
        update = rng.randrange(100) < int(self.p["update_pct"])
        lo, hi = self.p["orders_per_day"]
        base = rng.randint(int(lo), int(hi))
        day = self.cut + timedelta(days=day_index)
        growth = 10000 + int(self.p["growth_pct"]) * 100 * (day_index - 1) // 365
        n = (base * WEEKDAY_PCT[day.weekday()] * MONTH_PCT[day.month - 1] * growth
             // 100_000_000)
        if day in _peak_days(day.year):
            n = n * PEAK_PCT // 100
        n = max(1, n)
        l_lo, l_hi = (int(x) for x in self.p["lines_per_order"])
        # An order never holds the same product twice, so a seed asking for
        # more lines than the catalogue carries gets the catalogue. Capped
        # HERE, in phase A, because the line ids are prefix sums of these
        # counts and phase B must write exactly as many lines as they promise.
        l_hi = min(l_hi, len(self.available(day_index)))
        l_lo = min(l_lo, l_hi)
        lines = []
        for _ in range(n):
            k = l_lo
            while k < l_hi and rng.randrange(100) < 45:
                k += 1
            lines.append(k)
        return rng, {"signups": signups, "update": update, "lines": lines}

    def plan(self, day_index: int) -> dict:
        if day_index not in self.plans:
            self.plans[day_index] = self._phase_a(day_index)[1]
        return self.plans[day_index]

    def base(self, day_index: int) -> tuple[int, int, int]:
        """(first customer id, first order id, first line id) for the day:
        prefix sums of phase A over every earlier day."""
        while len(self.bases) <= day_index:
            i = len(self.bases) - 1
            plan = self.plan(i)
            c, o, n_lines = self.bases[i]
            self.bases.append((c + plan["signups"], o + len(plan["lines"]),
                               n_lines + sum(plan["lines"])))
        return self.bases[day_index]

    # ---- accounts ------------------------------------------------------
    def signup_of(self, customer_id: int) -> date:
        """The business date an account signed up on.

        A cutover account carries its own (customer_profile); a later one is
        found by locating its id among the days' prefix sums - the day whose
        signup range holds it. Only ever asked about ids that already exist,
        so the prefix sums it needs are already computed."""
        prof = self.profile(customer_id)
        if prof["signup_date"] is not None:
            return prof["signup_date"]
        firsts = [b[0] for b in self.bases]
        day_index = bisect.bisect_right(firsts, customer_id) - 1
        return self.cut + timedelta(days=day_index)

    def profile(self, customer_id: int) -> dict:
        if customer_id not in self.profiles:
            self.profiles[customer_id] = customer_profile(
                self.seed, customer_id, self.cut, self.initial)
        return self.profiles[customer_id]

    def _pick_customer(self, rng: random.Random, roster: int) -> int:
        """An account weighted by its activity, among the first `roster` ids."""
        while len(self.cum) < roster:
            cid = FIRST_CUSTOMER_ID + len(self.cum)
            prev = self.cum[-1] if self.cum else 0
            self.cum.append(prev + self.profile(cid)["activity"])
        pick = rng.randrange(self.cum[roster - 1])
        return FIRST_CUSTOMER_ID + bisect.bisect_right(self.cum, pick, 0, roster)

    # ---- catalogue -----------------------------------------------------
    @staticmethod
    def price_on(product_id: int, list_price: int, day_index: int) -> int:
        price = list_price
        for when, pid, new in PRICE_CHANGES:
            if pid == product_id and when <= day_index:
                price = new
        return price

    def available(self, day_index: int) -> list[tuple]:
        out = []
        for pid, _sku, _name, _cat, price, pop, kind in CATALOG:
            if LAUNCHES.get(pid, 0) > day_index:
                continue
            if pid in DISCONTINUED and DISCONTINUED[pid] <= day_index:
                continue
            out.append((pid, pop, kind, self.price_on(pid, price, day_index)))
        return out

    def catalogue_rows(self, day_index: int) -> list[dict]:
        """Catalogue changes effective on this day (all of it at cutover)."""
        day = self.cut + timedelta(days=day_index)
        rows = []
        for pid, sku, name, cat, price, _pop, _kind in CATALOG:
            launch = LAUNCHES.get(pid, 0)
            current = self.price_on(pid, price, day_index)
            retired = pid in DISCONTINUED and DISCONTINUED[pid] <= day_index
            if day_index == 0 and launch == 0:
                at = 0
            elif launch == day_index:
                at = 9 * 3600
            elif any(w == day_index and p == pid for w, p, _ in PRICE_CHANGES):
                at = 6 * 3600
            elif DISCONTINUED.get(pid) == day_index:
                at = 17 * 3600
            else:
                continue
            rows.append({"product_id": pid, "sku": sku, "product_name": name,
                         "category": cat, "unit_price": money(current),
                         "is_active": "false" if retired else "true",
                         "updated_at": _stamp(day, at)})
        return rows

    # ---- phase B: the content -----------------------------------------
    def content(self, day_index: int) -> dict:
        if day_index in self.contents:
            return self.contents[day_index]
        rng, plan = self._phase_a(day_index)
        self.plans.setdefault(day_index, plan)
        first_c, first_o, first_l = self.base(day_index)
        day = self.cut + timedelta(days=day_index)
        out = {"customers": [], "orders": [], "order_lines": [], "changes": [],
               "products": self.catalogue_rows(day_index)}

        if day_index == 0:
            for cid in range(FIRST_CUSTOMER_ID, first_c):
                prof = self.profile(cid)
                out["customers"].append(self._customer_row(
                    prof, prof["segment"], prof["signup_date"], _stamp(day, 0)))
            self.contents[day_index] = out
            return out

        # Signups. A new account's signup_date is today.
        for k in range(plan["signups"]):
            prof = self.profile(first_c + k)
            out["customers"].append(self._customer_row(
                prof, prof["segment"], day,
                _stamp(day, 8 * 3600 + rng.randrange(10 * 3600))))
        # An account changes segment - an upgrade, re-sent in full (SCD1
        # downstream: dim_customer carries the latest segment, so the account's
        # whole history moves with it).
        if plan["update"]:
            prof = self.profile(FIRST_CUSTOMER_ID + rng.randrange(first_c - FIRST_CUSTOMER_ID))
            out["customers"].append(self._customer_row(
                prof, UPGRADE[prof["segment"]], self.signup_of(prof["customer_id"]),
                _stamp(day, 9 * 3600 + rng.randrange(8 * 3600))))
        out["customers"].sort(key=lambda r: (r["updated_at"], r["customer_id"]))

        # Orders: times first, so ids increase with time as a real sequence does.
        roster = first_c - FIRST_CUSTOMER_ID + plan["signups"]
        times = sorted(rng.randrange(86400) for _ in plan["lines"])
        products = self.available(day_index)
        cancel_pm = int(self.p["cancel_pct"]) * 10
        refund_pm = cancel_pm + int(self.p["refund_pct"]) * 10
        line_id = first_l
        for j, (n_lines, sec) in enumerate(zip(plan["lines"], times, strict=False)):
            order_id = first_o + j
            cid = self._pick_customer(rng, roster)
            prof = self.profile(cid)
            segment = prof["segment"]
            ordered_at = _stamp(day, sec)
            subtotal, kinds = 0, set()
            pool = [(pid, pop * AFFINITY[segment][kind], kind, price)
                    for pid, pop, kind, price in products]
            for _ in range(min(n_lines, len(pool))):
                total = sum(w for _, w, _, _ in pool)
                pick = rng.randrange(total)
                # the loop variables are read AFTER the break: the picked line
                for idx, (pid, w, kind, price) in enumerate(pool):  # noqa: B007
                    if pick < w:
                        break
                    pick -= w
                pool.pop(idx)
                qty = self._quantity(rng, segment, kind)
                amount = qty * price
                subtotal += amount
                kinds.add(kind)
                out["order_lines"].append({
                    "order_line_id": line_id, "order_id": order_id, "product_id": pid,
                    "quantity": qty, "unit_price": money(price),
                    "line_amount": money(amount), "created_at": ordered_at})
                line_id += 1
            if kinds == {"service"}:
                shipping = 0
            elif segment == "Consumer":
                shipping = 0 if subtotal >= FREE_SHIP_CONSUMER else SHIP_CONSUMER[prof["region"]]
            else:
                shipping = 0 if subtotal >= FREE_SHIP_BUSINESS else SHIP_BUSINESS[prof["region"]]
            row = {"order_id": order_id, "customer_id": cid, "ordered_at": ordered_at,
                   "status": "completed", "subtotal": money(subtotal),
                   "shipping": money(shipping), "currency": "USD",
                   "updated_at": ordered_at}
            fate = rng.randrange(1000)
            if fate < cancel_pm:
                if rng.randrange(10) < 7:
                    # Cancelled within the hour, before the day's extract ran.
                    left = 86399 - sec
                    row["status"] = "cancelled"
                    row["updated_at"] = _stamp(day, sec + min(left, 60 + rng.randrange(3600)))
                else:
                    out["changes"].append((day_index + 1, rng.randrange(86400), row, "cancelled"))
            elif fate < refund_pm:
                lag = rng.choice(REFUND_LAGS)
                out["changes"].append((day_index + lag, rng.randrange(86400), row, "refunded"))
            out["orders"].append(row)
        self.contents[day_index] = out
        return out

    @staticmethod
    def _quantity(rng: random.Random, segment: str, kind: str) -> int:
        if segment == "Consumer":
            return 1 if rng.randrange(10) < 9 else 2
        if segment == "SMB":
            return 1 if kind == "service" else 1 + rng.randrange(4)
        if kind == "service":
            return 1 + rng.randrange(3)
        return 2 + rng.randrange(14) if kind == "furniture" else 2 + rng.randrange(19)

    @staticmethod
    def _customer_row(prof: dict, segment: str, signup: date, updated_at: str) -> dict:
        return {"customer_id": prof["customer_id"], "customer_name": prof["customer_name"],
                "email": prof["email"], "segment": segment, "region": prof["region"],
                "country": prof["country"], "signup_date": signup.isoformat(),
                "updated_at": updated_at}

    # ---- the extract ---------------------------------------------------
    def extract(self, day_index: int) -> dict:
        """The four files of day_index's extract, as row lists."""
        today = self.content(day_index)
        day = self.cut + timedelta(days=day_index)
        orders = list(today["orders"])
        for earlier in range(max(1, day_index - MAX_LAG), day_index):
            for due, sec, row, status in self.content(earlier)["changes"]:
                if due == day_index:
                    orders.append({**row, "status": status, "updated_at": _stamp(day, sec)})
        orders.sort(key=lambda r: (r["updated_at"], r["order_id"]))
        return {"orders": orders, "order_lines": today["order_lines"],
                "customers": today["customers"], "products": today["products"]}


_SHOPS: dict[str, _Shop] = {}


def shop(seed: dict) -> _Shop:
    params = params_of(seed)
    key = _key(params)
    if key not in _SHOPS:
        _SHOPS[key] = _Shop(params)
    return _SHOPS[key]


# --------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------

def write_day(workspace: Path, seed: dict, day_index: int, day) -> dict:
    """One night's extract: four CSVs under data/landing/<entity>/dt=<business date>/.

    `day` is the landing date the scheduler passes (vtime_start + day_index);
    the partition is named for the business date the extract COVERS, the day
    before. Pure in (seed, day_index): the live arrival stream and the initial
    build write identical bytes for day N, whatever order they got there in.
    Every entity gets a file every night, header-only on a quiet one - an
    extract that ran and found nothing says so, which is different from an
    extract that did not run.
    """
    s = shop(seed)
    bdate = business_date(s.p, day_index)
    rows = s.extract(day_index)
    produced = {"partition": f"dt={bdate.isoformat()}"}
    for entity in ENTITIES:
        folder = Path(workspace) / "data" / "landing" / entity / f"dt={bdate.isoformat()}"
        folder.mkdir(parents=True, exist_ok=True)
        # newline="" so the bytes are LF on every host: the landing zone is
        # fingerprinted workspace state.
        with (folder / f"{entity}.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS[entity], lineterminator="\n")
            w.writeheader()
            w.writerows(rows[entity])
        produced[entity] = len(rows[entity])
    return produced


def write_landing(workspace: Path, seed: dict) -> None:
    """The cutover snapshot and every nightly extract up to the world clock."""
    start = parse_vtime(seed["vtime_start"])
    for i in range(seed["days"]):
        write_day(workspace, seed, i, (start + timedelta(days=i)).date())
    write_ops_logs(workspace, seed)


# --------------------------------------------------------------------------
# historical ops logs (var/logs)
# --------------------------------------------------------------------------

def write_ops_logs(workspace: Path, seed: dict) -> None:
    """The platform-side record of the seeded backstory: three rotated daemon
    logs under var/logs/, covering every seeded night on the virtual clock.

    - ingest_daemon.log   the shop platform's CDC extract daemon sealing each
                          night's four-file extract at 02:00
    - sftp_sync.log       the sync that pushes those files to the landing zone
                          (sizes and checksums of what actually landed)
    - scheduler_audit.log the data platform's audit trail of the night's three
                          job runs (02:30 daily_load, 02:40 dbt_build, 06:00
                          bi_refresh) processing the business date just landed

    A pure function of the seed: jitter comes from one stream per night,
    `Random(f"{seed}:logs:{day_index}")`, and every timestamp is derived from
    seed['vtime_start'] - never a wall clock. Two builds of one seed write
    byte-identical logs. Called from write_landing (build time) only: the live
    arrival stream calls write_day directly and appends nothing here.
    """
    s = shop(seed)
    start = parse_vtime(seed["vtime_start"])
    folder = Path(workspace).parent / "var" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    ingest, sftp, audit = [], [], []
    for i in range(seed["days"]):
        rng = random.Random(f"{seed['seed']}:logs:{i}")
        run_day = start + timedelta(days=i)
        bdate = business_date(s.p, i)           # the business date the extract covers
        rows = s.extract(i)
        counts = {e: len(rows[e]) for e in ENTITIES}

        # the extract daemon: sealed at 02:00 (+jitter), after the source poll
        seal = run_day + timedelta(hours=2, seconds=rng.randrange(4, 40))
        elapsed = rng.randrange(9, 28)
        if rng.randrange(100) < 4:
            ingest.append(
                f"{scheduler.iso(seal - timedelta(seconds=elapsed))} WARN "
                f"ingestd[1142]: source cursor on orders stalled "
                f"{rng.randrange(2, 9)}s; resumed without loss")
        ingest.append(
            f"{scheduler.iso(seal)} INFO ingestd[1142]: extract sealed "
            f"dt={bdate.isoformat()} "
            + " ".join(f"{e}={counts[e]}" for e in ENTITIES)
            + f" elapsed_s={elapsed}")
        if counts["customers"] == 0:
            ingest.append(
                f"{scheduler.iso(seal + timedelta(seconds=1))} INFO "
                f"ingestd[1142]: quiet night on customers "
                f"dt={bdate.isoformat()} - header-only file delivered")

        # the sftp sync: one push per file, of the bytes that actually landed
        sent = seal + timedelta(seconds=rng.randrange(20, 90))
        for entity in ENTITIES:
            file = (Path(workspace) / "data" / "landing" / entity
                    / f"dt={bdate.isoformat()}" / f"{entity}.csv")
            data = file.read_bytes()
            if rng.randrange(100) < 3:
                sftp.append(
                    f"{scheduler.iso(sent)} WARN sftp-sync[88]: push "
                    f"{entity}/dt={bdate.isoformat()}/{entity}.csv stalled on "
                    f"first ACK; re-sending")
                sent += timedelta(seconds=rng.randrange(4, 15))
            sftp.append(
                f"{scheduler.iso(sent)} INFO sftp-sync[88]: push "
                f"{entity}/dt={bdate.isoformat()}/{entity}.csv "
                f"bytes={len(data)} sha256={hashlib.sha256(data).hexdigest()[:16]} "
                f"attempts=1 ok")
            sent += timedelta(seconds=rng.randrange(1, 9))

        # the scheduler's audit trail of the night's three runs
        for name, hhmm, nominal in (("daily_load", "02:30", 75),
                                    ("dbt_build", "02:40", 240),
                                    ("bi_refresh", "06:00", 420)):
            hh, mm = (int(x) for x in hhmm.split(":"))
            begin = run_day + timedelta(hours=hh, minutes=mm,
                                        seconds=rng.randrange(0, 45))
            took = max(5, nominal + rng.randrange(-nominal // 6, nominal // 5))
            level, note = "INFO", ""
            if rng.randrange(100) < 4:
                took = int(nominal * 1.6) + rng.randrange(30)
                level, note = "WARN", f" above_p90={int(nominal * 1.5)}"
            audit.append(
                f"{scheduler.iso(begin)} INFO sched-audit[7]: job={name} "
                f"trigger=scheduled biz_dt={bdate.isoformat()} started")
            audit.append(
                f"{scheduler.iso(begin + timedelta(seconds=took))} {level} "
                f"sched-audit[7]: job={name} status=success "
                f"duration_s={took}{note}")

    for name, lines in (("ingest_daemon.log", ingest),
                        ("sftp_sync.log", sftp),
                        ("scheduler_audit.log", audit)):
        # newline="\n" like the landing CSVs: var/logs sits beside the
        # fingerprinted planes, so the bytes are LF on every host.
        (folder / name).write_text("".join(f"{line}\n" for line in lines),
                                   encoding="utf-8")


def load_raw(workspace: Path, db_path: Path, landing: Path | None = None) -> None:
    """Run the workspace's own loader, so the script the agent reads is the
    code that loads raw.* - at build time and every night after. `landing`
    defaults to the workspace's own landing zone; `bi_imports` replays a night
    on the partitions that had landed by then."""
    loader = Path(workspace) / "loader" / "load_extracts.py"
    r = subprocess.run(
        [sys.executable, str(loader),
         "--landing", str(landing or Path(workspace) / "data" / "landing"),
         "--db", str(db_path)],
        stdin=subprocess.DEVNULL,  # never inherit the MCP JSON-RPC pipe; see executor.py
        capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"load_extracts failed:\n{r.stdout}\n{r.stderr}")


# --------------------------------------------------------------------------
# the BI refresh
# --------------------------------------------------------------------------

DB = Path("var") / "warehouse" / "warehouse.duckdb"
DASHBOARDS = Path("var") / "bi" / "dashboards"
REFRESH_OUT = Path("build") / "bi" / "refresh"


def refresh_bi(root: Path) -> dict:
    """The 06:00 dashboard refresh: the workspace's own
    scripts/refresh_dashboards.py, so the code the agent reads is the code the
    `bi_refresh` job runs (the same rule load_raw follows).

    The script renders every dashboard deployed under var/bi/dashboards/
    against the marts as they stand (a panel that no longer binds fails the
    run with DuckDB's error), writes the values to build/bi/refresh/ - the BI
    cache, outside the fingerprint and the snapshot, like dbt's target/ - and
    appends the refresh to raw.bi_refresh_history with the data watermark it
    saw (max(order_date) in warehouse.fct_orders). That table is created by
    the first refresh a world executes; the seeded history predates it, so at
    build time the obs readings over it are honestly null.

    A world with no dashboards deployed (a profile without the `bi` slot)
    FAILS rather than reporting a refresh of nothing.

    Returns counts only - deterministic, so safe inside the fingerprinted run
    record. The rendered values are `world.bi.render`'s for the same warehouse
    state (test_theme_commerce_analytics asserts the equality).
    """
    root = Path(root)
    script = root / "workspace" / "scripts" / "refresh_dashboards.py"
    r = subprocess.run(
        [sys.executable, str(script),
         "--dashboards", str(root / DASHBOARDS),
         "--db", str(root / DB),
         "--out", str(root / REFRESH_OUT)],
        stdin=subprocess.DEVNULL,  # never inherit the MCP JSON-RPC pipe; see executor.py
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"refresh_dashboards failed:\n{r.stdout}{r.stderr}")
    summary = json.loads(r.stdout.strip().splitlines()[-1])
    return {k: summary[k] for k in ("dashboards", "panels", "refresh_id")}


# --------------------------------------------------------------------------
# the human-context plane
# --------------------------------------------------------------------------

def write_workflow(root: Path, seed: dict, manifest: dict) -> None:
    """Seed var/workflow/{issues,chat,kb}.json per gateway/SURFACES.md#workflow.

    The corpus is authored as data in this theme's workflow/ directory and
    dated against the world clock, which is read out of the scheduler state
    `build` has already written rather than re-derived from the seed - one
    definition of `vnow`, so a ticket cannot be filed after today.
    """
    workflow.seed(root, Path(__file__).resolve().parent, manifest["workflow"],
                  scheduler.load(root)["vnow"])


# --------------------------------------------------------------------------
# the BI projects (world/world/bi_projects.py)
# --------------------------------------------------------------------------

def bi_projects(seed: dict) -> dict:
    """What each BI vendor's project in workspace/bi/ is, when the seed's
    profile plugs that vendor: {vendor: (Model, (Report, ...))}.

    One model and one report per tool, because the Executive Overview exists
    three times: leadership's in Power BI, the copy Sales rebuilt in Tableau
    when they moved there, and the copy Finance rebuilt in Sigma for the close
    (exposures.yml, kb ae/architecture/analytics-stack). Each is the `Commerce`
    semantic model over the five `warehouse` marts (the star schema
    world/world/semantic/examples.py fixes and this theme's dbt project
    builds) and the `Executive Overview` report. Power BI gets the model as
    written in DAX terms; Tableau and Sigma get their editions
    (formats/tableau/examples.py, formats/sigma/examples.py), because the DAX
    model's CALCULATE overwrite, ALL(segment) and time intelligence have no
    exact Tableau calculation or Sigma formula, and a front-end refuses rather
    than approximates. On every visual the three share they compute the same
    numbers on this warehouse (world/tests/test_bi_projects.py proves it).

    `bi_faults` (seed key, OFF by default, BI_FAULTS below): defects planted in
    a vendor's project as FIRST COMMITTED, so the vendor's fake service
    publishes them - a dashboard that is wrong in production and wrong in git.
    With no `bi_faults` the projects are exactly the editions above, byte for
    byte, and every recorded hash stands.

    Otherwise pure: the star schema's names do not depend on the seed's scale
    knobs.
    """
    from world.semantic import examples
    from world.semantic.formats.sigma import examples as sigma_edition
    from world.semantic.formats.tableau import examples as tableau_edition

    projects = {
        "powerbi": (examples.commerce_model("dax"), (examples.executive_report(),)),
        "tableau": (tableau_edition.tableau_commerce_model(),
                    (tableau_edition.tableau_executive_report(),)),
        "sigma": (sigma_edition.commerce_model(), (sigma_edition.executive_report(),)),
    }
    for fault in bi_faults(seed):
        vendor, plant = BI_FAULTS[fault]
        model, reports = projects[vendor]
        projects[vendor] = plant(model, reports)
    return projects


# --------------------------------------------------------------------------
# BI faults: defects a seed can switch on in a vendor's committed project
# --------------------------------------------------------------------------
#
# Each is a mistake the team that owns that build made in its own tool, and
# each leaves the tool computing a number that disagrees with the dbt metric of
# the same name (dbt_project/models/semantic/metrics.yml) while every
# calculation still reads as plausible. Nothing in the warehouse, the dbt
# project or the pipeline changes: the marts are right, and so is Power BI.
#
#   tableau_orders_line_join   Sales' Tableau data source builds its `Orders`
#                              logical table from Custom SQL that joins
#                              fct_orders to fct_order_lines (so the order
#                              sheets could show product and quantity), with no
#                              separate Order Lines table. Every order-grain
#                              measure is summed once per LINE: Net Revenue,
#                              Gross Revenue and Completed Orders are fanned
#                              out; Units and Top Products are right. The
#                              calculated fields are the correct formulas.
#   sigma_net_revenue_refunds  Finance's Sigma workbook does not use the data
#                              model's Net Revenue metric: every element
#                              computes it inline as order_amount where the
#                              status is not `cancelled` - net of cancellations
#                              only, so refunded orders count. AOV on the KPI
#                              card divides that by Completed Orders. The data
#                              model's own metrics are correct.
#   powerbi_share_scope        Leadership's Power BI model: the Segment Share
#                              measure's denominator removes EVERY filter on
#                              Customer - REMOVEFILTERS ( Customer ) - instead
#                              of the segment's alone (docs/metrics.md: "Net
#                              Revenue with the segment filter removed").
#                              Unfiltered, the two are the same number, so the
#                              Executive Overview's Revenue by Segment tile is
#                              right; filtered to a region, each segment's
#                              share is of the WHOLE book, and a region's
#                              shares add up to its slice of revenue, not 100%.
#   powerbi_stale_import       Not a line in the project (which is the clean
#                              one): the night the world's last scheduled
#                              Power BI refresh ran BEFORE the last nightly
#                              extract landed. The extract lands late by the
#                              catalogue's own `extract_late` delay, after the
#                              06:00 refresh; the data platform's on-call
#                              reruns daily_load and dbt_build by hand, so the
#                              warehouse is complete - and nobody refreshes
#                              the dataset. `bi_imports` below writes what that
#                              refresh imported, by replaying the load and the
#                              dbt build on the extracts that had landed, and
#                              the Power BI service publishes it.
#
# Seed: `bi_faults: [tableau_orders_line_join, sigma_net_revenue_refunds]`
# (a list; any of the ids). Refused: an unknown id, and a fault in a vendor the
# seed's profile does not plug (bi_projects.write would drop the project and
# the fault with it, silently).

#: The Custom SQL Sales' Orders logical table is built from. One row per LINE.
TABLEAU_ORDERS_LINE_SQL = """SELECT
  o.order_id, o.order_date, o.customer_id, o.order_status,
  o.order_amount, o.shipping_amount,
  l.order_line_id, l.product_id, l.quantity, l.line_amount
FROM warehouse.fct_orders AS o
INNER JOIN warehouse.fct_order_lines AS l ON l.order_id = o.order_id"""


def _tableau_orders_line_join(model, reports):
    """Sales' data source: Orders = fct_orders joined to its lines, as Custom
    SQL; no Order Lines table; Product related to Orders through the line's
    product_id. The measures keep their formulas; the two line measures (Units,
    List Price Revenue) now live on Orders, which is where the joined
    quantity column is."""
    from dataclasses import replace

    from world.semantic.expr import Agg, BinOp, Col, IterAgg, Related
    from world.semantic.model import Column, Relationship, Table

    def column(name, dtype, **kw):
        return Column(name=name, source=name, data_type=dtype, **kw)

    orders = Table("Orders", None, (
        column("order_id", "int"), column("order_date", "date"),
        column("customer_id", "int", hidden=True), column("order_status", "string"),
        column("order_amount", "decimal"), column("shipping_amount", "decimal"),
        column("order_line_id", "int"), column("product_id", "int", hidden=True),
        column("quantity", "int"), column("line_amount", "decimal")),
        source_sql=TABLEAU_ORDERS_LINE_SQL)
    tables = tuple(orders if t.name == "Orders" else t for t in model.tables
                   if t.name != "Order Lines")
    relationships = (
        Relationship("Orders", "customer_id", "Customer", "customer_id"),
        Relationship("Orders", "order_date", "Date", "date_day"),
        Relationship("Orders", "product_id", "Product", "product_id"),
    )
    line_measures = {
        "Units": Agg("sum", Col("Orders", "quantity")),
        "List Price Revenue": IterAgg("sum", "Orders",
                                      BinOp("*", Col("Orders", "quantity"),
                                            Related("Product", "unit_price"))),
    }
    measures = tuple(replace(m, table="Orders", expr=line_measures[m.name])
                     if m.name in line_measures else m for m in model.measures)
    return replace(model, tables=tables, relationships=relationships,
                   measures=measures), reports


def _sigma_net_revenue_refunds(model, reports):
    """Finance's workbook: Net Revenue inline as SumIf(order_amount, status !=
    "cancelled") on every element, and AOV as that over Completed Orders. The
    data model is untouched."""
    from dataclasses import replace

    from world.semantic.expr import BinOp, Col, Divide, If, IterAgg, Lit, MeasureRef

    net = IterAgg("sum", "Orders",
                  If(BinOp("<>", Col("Orders", "order_status"), Lit("cancelled")),
                     Col("Orders", "order_amount")))
    inline = {"Net Revenue": net, "AOV": Divide(net, MeasureRef("Completed Orders"))}

    def visual(v):
        return replace(v, values=tuple((label, inline.get(label, expr))
                                       for label, expr in v.values))

    return model, tuple(
        replace(r, pages=tuple(replace(p, visuals=tuple(visual(v) for v in p.visuals))
                               for p in r.pages))
        for r in reports)


def _powerbi_share_scope(model, reports):
    """Leadership's model: Segment Share = DIVIDE ( [Net Revenue], CALCULATE (
    [Net Revenue], REMOVEFILTERS ( Customer ) ) ) - every Customer filter
    removed, the region's with the segment's. The report is untouched."""
    from dataclasses import replace

    from world.semantic.expr import Calculate, Col, Divide, RemoveFilters

    segment = RemoveFilters(columns=(Col("Customer", "segment"),))

    def widen(measure):
        expr = measure.expr
        if measure.name != "Segment Share" or not (
                isinstance(expr, Divide) and isinstance(expr.den, Calculate)
                and segment in expr.den.modifiers):
            return measure
        modifiers = tuple(RemoveFilters(tables=("Customer",)) if m == segment else m
                          for m in expr.den.modifiers)
        return replace(measure, expr=replace(expr, den=replace(expr.den,
                                                               modifiers=modifiers)))

    measures = tuple(widen(m) for m in model.measures)
    if measures == model.measures:
        raise SystemExit("powerbi_share_scope: the Power BI model has no Segment Share "
                         "that removes the segment filter to widen")
    return replace(model, measures=measures), reports


def _the_project_as_it_is(model, reports):
    """powerbi_stale_import: the project is the clean one; the fault is the
    world's timeline, written by `bi_imports`."""
    return model, reports


#: fault id -> (the vendor whose project it is planted in, the planter).
BI_FAULTS = {
    "tableau_orders_line_join": ("tableau", _tableau_orders_line_join),
    "sigma_net_revenue_refunds": ("sigma", _sigma_net_revenue_refunds),
    "powerbi_share_scope": ("powerbi", _powerbi_share_scope),
    "powerbi_stale_import": ("powerbi", _the_project_as_it_is),
}


def bi_faults(seed: dict) -> tuple[str, ...]:
    """The seed's `bi_faults`, validated, in catalogue order; () when absent."""
    named = seed.get("bi_faults") or ()
    if isinstance(named, str):
        named = (named,)
    if not isinstance(named, (list, tuple)) or not all(isinstance(f, str) for f in named):
        raise SystemExit(f"seed `bi_faults` must be a list of fault ids, got {named!r}")
    unknown = sorted(set(named) - set(BI_FAULTS))
    if unknown:
        raise SystemExit(f"seed `bi_faults` names unknown fault(s) {unknown}; "
                         f"commerce_analytics declares {sorted(BI_FAULTS)}")
    plugged = set(((seed.get("profile") or {}).get("plugs") or {}).get("bi") or ())
    for fault in named:
        vendor = BI_FAULTS[fault][0]
        if vendor not in plugged:
            raise SystemExit(f"seed `bi_faults` plants {fault!r} in {vendor}, but the "
                             f"seed's profile does not plug {vendor} "
                             f"(profile.plugs.bi: {sorted(plugged)})")
    return tuple(f for f in BI_FAULTS if f in set(named))


# --------------------------------------------------------------------------
# the Power BI import: what the world's last scheduled refresh loaded
# --------------------------------------------------------------------------
#
# The Commerce dataset is an IMPORT model (workspace bi/README.md): the report
# shows what its last refresh copied out of the warehouse, and in this world
# its scheduled refresh is the 06:00 dashboard refresh (`bi_refresh`,
# theme.yaml; README.md "Power BI / Tableau / Sigma <--06:00"; the January
# postmortem). The fake Power BI service (gateway/src/alkera_gateway/native/
# services/powerbi.py) seeds a dataset's import from POWERBI_IMPORTS when the
# world carries a record for it, and otherwise copies the warehouse at
# provisioning - which IS what the last scheduled refresh loaded on a world
# whose every extract landed before that refresh. So only a world where that
# is not true needs a record, and only `powerbi_stale_import` writes one:
# every other world keeps every byte it had (bi_projects.write calls this
# hook, which returns [] for it).
#
# The record is the world's own timeline, replayed, not a number planted:
#
#   - the night is the one before the world clock, so nothing after it can
#     have refreshed the dataset: the last seeded extract (business date
#     2026-02-28 in the shipped seed) is due at theme.yaml `live.arrival`
#     (02:00) and lands `extract_late`'s `delay_minutes` later (07:30);
#   - the load and the refresh are the seeded runs of that night in
#     var/sched/state.json - `daily_load` 02:30, `bi_refresh` 06:00 - and the
#     import is what that load and the dbt build after it made of the
#     partitions that had landed by then: the workspace's own loader and dbt
#     project, run on a scratch warehouse, the relations the published model
#     reads exported as CSV (canonical order, DuckDB's own text for each type);
#   - the warehouse is complete because the data platform's on-call reran
#     the load and the build by hand RERUN_LAG after the extract landed
#     (ae/runbooks/nightly-on-call), and stopped there - the step the January
#     postmortem's action 3 never wrote down. The two manual runs are appended
#     to the scheduler's history in the shape `trigger` records, with the row
#     counts the replay and the build actually measured as their effects.
#
# CSV, not a DuckDB file: var/bi is hashed byte for byte (fingerprint.py
# `bi`), and a DuckDB file's page layout is not.

#: Where the world keeps the Power BI imports it can vouch for (beside the
#: dashboards plane the gateway's `dashboards` path names).
POWERBI_IMPORTS = Path("var") / "bi" / "powerbi"
POWERBI_RECORD = "datasets.json"
#: Landing -> the on-call's manual `daily_load`: the time to notice the
#: extract (#data-incidents) and start the rerun. Author's choice, stated.
RERUN_LAG = timedelta(minutes=35)
#: How many scheduled refreshes the record carries as the dataset's history.
REFRESH_HISTORY = 14
#: The fault whose delay dates the late extract (theme.yaml live.faults).
LATE_EXTRACT_FAULT = "extract_late"


def _late_extract_night(seed: dict, manifest: dict, state: dict) -> dict:
    """The night before the world clock, when the last extract landed after
    the refresh: every time on it read from theme.yaml and the seeded runs."""
    params = params_of(seed)
    start = parse_vtime(seed["vtime_start"])
    arrival = manifest["live"]["arrival"]["at"]
    hh, mm = (int(x) for x in arrival["time"].split(":"))

    def due(day_index: int) -> datetime:
        return start + timedelta(days=day_index + int(arrival.get("day_offset", 0)),
                                 hours=hh, minutes=mm)

    late = int(seed["days"]) - 1
    fault = next((f for f in manifest["live"].get("faults") or ()
                  if f.get("id") == LATE_EXTRACT_FAULT and f.get("kind") == "late_arrival"),
                 None)
    if fault is None:
        raise SystemExit(f"powerbi_stale_import dates the late extract by theme.yaml's "
                         f"{LATE_EXTRACT_FAULT!r} late_arrival fault, which is not declared")
    landed = due(late) + timedelta(minutes=int(fault["params"]["delay_minutes"]))
    vnow = parse_vtime(state["vnow"])
    refreshes = [r for r in state["runs"] if r["job"] == "bi_refresh"
                 and parse_vtime(r["vts_start"]) < vnow]
    if not refreshes:
        raise SystemExit("powerbi_stale_import: the seeded history has no bi_refresh run")
    refresh = refreshes[-1]
    at = parse_vtime(refresh["vts_start"])
    loads = [r for r in state["runs"] if r["job"] == "daily_load"
             and parse_vtime(r["vts_start"]) <= at]
    if not loads:
        raise SystemExit("powerbi_stale_import: no daily_load ran before the last refresh")
    load = parse_vtime(loads[-1]["vts_start"])
    if not (due(late) <= load < at < landed < vnow) or refresh["status"] != "success":
        raise SystemExit(
            f"powerbi_stale_import: the timeline does not stage it - extract due "
            f"{scheduler.iso(due(late))}, load {scheduler.iso(load)}, refresh "
            f"{refresh['vts_start']} ({refresh['status']}), landed {scheduler.iso(landed)}, "
            f"world clock {state['vnow']}")
    landed_by_load = [f"dt={business_date(params, i).isoformat()}"
                      for i in range(int(seed["days"]))
                      if (landed if i == late else due(i)) <= load]
    return {"partition": f"dt={business_date(params, late).isoformat()}",
            "due": due(late), "landed": landed, "load": load, "refresh": refresh,
            "partitions": landed_by_load, "history": refreshes[-REFRESH_HISTORY:]}


def _replay_warehouse(root: Path, scratch: Path, manifest: dict, partitions: list) -> Path:
    """The warehouse as `daily_load` + `dbt_build` left it with only
    `partitions` on disk: the workspace's own loader and dbt project, on a
    scratch copy of those partitions. Returns the scratch warehouse."""
    from world import executor

    workspace = Path(root) / "workspace"
    landing = scratch / "landing"
    for entity in ENTITIES:
        for partition in partitions:
            source = workspace / "data" / "landing" / entity / partition
            if source.is_dir():
                shutil.copytree(source, landing / entity / partition)
    db = scratch / DB
    db.parent.mkdir(parents=True, exist_ok=True)
    load_raw(workspace, db, landing=landing)
    shutil.copytree(workspace / "dbt_project", scratch / "workspace" / "dbt_project")
    rc, output = executor.dbt_invoke(scratch, "build", out_dir=scratch / "build",
                                     profile=manifest["dbt_profile"])
    if rc != 0:
        raise SystemExit(f"powerbi_stale_import: the replayed dbt build failed:\n{output}")
    return db


def _row_counts(db: Path, relations) -> dict:
    import duckdb

    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh (READ_ONLY)")
        return {r: con.execute(f"SELECT count(*) FROM wh.{r}").fetchone()[0]
                for r in relations}
    finally:
        con.close()


def _export_import(root: Path, dataset: str, db: Path, relations) -> list[dict]:
    """Each relation as CSV under POWERBI_IMPORTS/import/<dataset>/, rows in
    canonical order; [{relation, path, columns: [[name, type]], rows}]."""
    import duckdb

    folder = Path(root) / POWERBI_IMPORTS / "import" / dataset
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh (READ_ONLY)")
        for relation in relations:
            schema, _, table = relation.partition(".")
            columns = [[name, dtype] for name, dtype in con.execute(
                "SELECT column_name, data_type FROM duckdb_columns() WHERE "
                "database_name = 'wh' AND schema_name = ? AND table_name = ? "
                "ORDER BY column_index", [schema, table]).fetchall()]
            if not columns:
                raise SystemExit(f"powerbi_stale_import: the replayed warehouse has no "
                                 f"{relation}")
            path = folder / f"{relation}.csv"
            target = str(path).replace("'", "''")
            con.execute(f'COPY (SELECT * FROM wh."{schema}"."{table}" ORDER BY ALL) '
                        f"TO '{target}' (FORMAT csv, HEADER true, DELIMITER ',')")
            rows = con.execute(f'SELECT count(*) FROM wh."{schema}"."{table}"').fetchone()[0]
            out.append({"relation": relation,
                        "path": path.relative_to(Path(root) / POWERBI_IMPORTS).as_posix(),
                        "columns": columns, "rows": rows})
    finally:
        con.close()
    return out


def _effects(tables, before: dict, after: dict) -> dict:
    return {t: {"rows_before": before[t], "rows_after": after[t]} for t in tables}


def bi_imports(root: Path, seed: dict, manifest: dict, db_path: Path) -> list[Path]:
    """bi_projects.write's import hook: the import-mode BI datasets as the
    world's last scheduled refresh left them, for the worlds where that is not
    simply the warehouse (above). Returns the files written; [] - and nothing
    written, nothing appended - unless `powerbi_stale_import` is on."""
    if "powerbi_stale_import" not in bi_faults(seed):
        return []
    root = Path(root)
    state = scheduler.load(root)
    night = _late_extract_night(seed, manifest, state)
    model = bi_projects(seed)["powerbi"][0]
    relations = []
    for table in model.tables:
        if table.source is None:
            raise SystemExit(f"powerbi_stale_import: table {table.name!r} is native SQL; "
                             f"the import replay copies named relations only")
        relations.append(table.source)
    relations = sorted(set(relations))
    jobs = manifest["jobs"]
    load_tables = list(jobs["daily_load"]["exec"]["tables"])
    dbt_tables = list(jobs["dbt_build"]["exec"]["tables"])
    with tempfile.TemporaryDirectory(prefix="powerbi-import-") as tmp:
        stale = _replay_warehouse(root, Path(tmp), manifest, night["partitions"])
        imported = _export_import(root, model.name, stale, relations)
        before = _row_counts(stale, load_tables + dbt_tables)
    after = _row_counts(Path(db_path), load_tables + dbt_tables)

    # The on-call's rerun: `trigger`'s run record (scheduler._run_record), the
    # effects measured, not asserted. The world clock does not move: it is the
    # backstory, like every seeded row.
    load_at = night["landed"] + RERUN_LAG
    load_end = load_at + timedelta(seconds=int(jobs["daily_load"]["duration_s"]))
    dbt_at = load_at + timedelta(minutes=int(jobs["dbt_build"]["after"]["minutes"]))
    dbt_end = dbt_at + timedelta(seconds=int(jobs["dbt_build"]["duration_s"]))
    if dbt_end >= parse_vtime(state["vnow"]):
        raise SystemExit("powerbi_stale_import: the rerun would end after the world clock")
    state["runs"] += [
        {"job": "daily_load", "vts_start": scheduler.iso(load_at),
         "vts_end": scheduler.iso(load_end), "status": "success", "detail": None,
         "trigger": "manual", "execution_date": None, "executed": True,
         "action": f"theme:{jobs['daily_load']['exec']['op']}", "error": None,
         "effects": _effects(load_tables, before, after)},
        {"job": "dbt_build", "vts_start": scheduler.iso(dbt_at),
         "vts_end": scheduler.iso(dbt_end), "status": "success", "detail": None,
         "trigger": "manual", "execution_date": None, "executed": True,
         "action": f"dbt {jobs['dbt_build']['exec']['command']}", "error": None,
         "effects": {"returncode": 0, **_effects(dbt_tables, before, after)}},
    ]
    scheduler.save(root, state)

    status = {"success": "Completed", "failed": "Failed"}
    record = {"datasets": {model.name: {
        "refreshes": [{"refreshType": "Scheduled", "startTime": r["vts_start"],
                       "endTime": r["vts_end"],
                       "status": status.get(r["status"], "Failed")}
                      for r in night["history"]],
        "import": {"refreshed": night["refresh"]["vts_start"], "relations": imported},
        "timeline": {"partition": night["partition"],
                     "due": scheduler.iso(night["due"]),
                     "landed": scheduler.iso(night["landed"]),
                     "reloaded": scheduler.iso(dbt_end)},
    }}}
    path = root / POWERBI_IMPORTS / POWERBI_RECORD
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
    folder = root / POWERBI_IMPORTS
    return sorted(p for p in folder.rglob("*") if p.is_file())
