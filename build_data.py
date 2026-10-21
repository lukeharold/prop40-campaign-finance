"""Pull Prop 40 contributions out of the Cal-Access bulk export and build
the CSVs that feed the Datawrapper charts.

Reads raw/dbwebexport.zip directly (no full unzip) and writes to output/.

Sources inside the export:
  CVR_CAMPAIGN_DISCLOSURE_CD  filing covers (filer, amendment, period)
  RCPT_CD                     Form 460 Schedules A and C (monetary, in-kind)
  EXPN_CD                     Form 460 Schedule E (payments made)
  S497_CD                     Form 497 24-hour late contribution reports

Method:
  * Keep only the latest amendment of each filing.
  * Use Form 460s for everything through each committee's latest 460
    period end, then add Form 497s dated after that, so late money is
    counted once.
  * Drop transfers between the six Prop 40 committees (double counting).
  * Maps use individual donors only: committee and organization money
    maps to office addresses, not to where donors are.
  * Building a Better California, the pass-through behind most No money,
    gets its own donor chart plus the share of its spending that went to
    No on 40. Its donors are not credited to Prop 40 directly.
  * Props 41 and 42 are the counter-measures: the same two camps fight all
    three. funding_*.csv and coalition.csv show who pays for each side,
    grouped by funder. Committees on several measures count in each.
"""

import csv
import io
import json
import re
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ZIP_PATH = HERE / "raw" / "dbwebexport.zip"
CROSSWALK = HERE / "ref" / "zcta_county.txt"
OUT = HERE / "output"

# Cal-Access filer IDs, from the Secretary of State's Prop 40 page.
COMMITTEES = {
    "1484470": ("Yes", "Yes on 40 - Billionaire Tax Now (SEIU-UHW)"),
    "1495036": ("Yes", "Tax the Ultra-Rich Now, Yes on 3 & 40"),
    "1492108": ("No", "No on Prop 40 - Doctors, Teachers, Small Businesses..."),
    "1486695": ("No", "Golden State Promise - No on Prop 40"),
    "1485007": ("No", "Stop the Squeeze - No on Prop 40"),
    "1485228": ("No", "Californians Against Tax Increases (CA Business Roundtable)"),
}
BBC_ID = "1486767"  # Building a Better California
BBC_SINCE = date(2025, 1, 1)
PROP40_NAME = re.compile(r"\b(PROP(OSITION)?\.? ?40|ON 40)\b")

# Every committee's position on Props 40, 41 and 42. From the SoS pages,
# plus 1484470, which the SoS lists only under Prop 40 but whose registered
# name is "Yes on 40 - Billionaire Tax Now - No on 41 & 42".
POSITIONS = {
    "1484470": {"40": "Yes", "41": "No", "42": "No"},
    "1495036": {"40": "Yes", "41": "No", "42": "No"},
    "1492108": {"40": "No"},
    "1486695": {"40": "No"},
    "1485007": {"40": "No"},
    "1485228": {"40": "No"},
    "1488423": {"41": "Yes"},  # Yes on 41, sponsored by Building a Better California
    "1488418": {"42": "Yes"},  # Yes on 42, sponsored by Building a Better California
    # Formed in October 2026; not yet on the SoS measure pages when added.
    "1497412": {"41": "No", "42": "No"},
}
MEASURES = ("40", "41", "42")
# The two camps: for the billionaire tax = Yes on 40, No on 41 and 42.
PRO_TAX = {"40": "Yes", "41": "No", "42": "No"}
FUNDERS = ("Building a Better California", "SEIU-UHW and its committees", "All other donors")
SEIU_NAME = re.compile(r"\bSEIU\b|SERVICE EMPLOYEES INTERNATIONAL UNION.*UNITED HEALTH ?CARE")


def funder(r):
    nm = r["name"].upper()
    if r["donor_cmte_id"] == BBC_ID or "BUILDING A BETTER CALIFORNIA" in nm:
        return FUNDERS[0]
    if SEIU_NAME.search(nm):
        return FUNDERS[1]
    return FUNDERS[2]


def camp(filer_id):
    """'pro' or 'anti' billionaire tax. Every position a committee takes must
    point to the same camp; anything else is a data-entry mistake."""
    camps = {"pro" if PRO_TAX[m] == side else "anti" for m, side in POSITIONS[filer_id].items()}
    if len(camps) != 1:
        raise ValueError(f"Committee {filer_id} has positions in both camps: {POSITIONS[filer_id]}")
    return camps.pop()


# Pie slices, in display order. Each committee goes in exactly one slice, so
# no committee's money is ever counted twice.
SLICES = [
    ("yes40", "Yes on 40 / No on 41 and 42"),
    ("multi", "Multiple measures: No on 40, Yes on 41 or 42"),
    ("no40", "No on 40"),
    ("yes41", "Yes on 41"),
    ("yes42", "Yes on 42"),
]


def slice_of(filer_id):
    """The one slice a committee belongs to. The Yes on 40 camp is a single
    slice. On the other side, a committee on one measure gets that measure's
    slice; a committee on more than one gets the combined 'multi' slice."""
    if camp(filer_id) == "pro":
        return "yes40"
    pos = POSITIONS[filer_id]
    if len(pos) > 1:
        return "multi"
    return {"40": "no40", "41": "yes41", "42": "yes42"}[next(iter(pos))]

csv.field_size_limit(sys.maxsize)


# Problems found while reading, reported (and fatal) in check_data().
# Nothing questionable is dropped or guessed at silently.
RUN = {"rows_read": defaultdict(int), "malformed": defaultdict(list),
       "bad_amounts": [], "undated_late": [], "duplicates_removed": []}


def read_tsv(zf, name, col=None, keep=None):
    """Yield dict rows from a TSV inside the zip. Cal-Access files are
    latin-1-ish, tab separated, and not reliably quoted.

    With col/keep, rows whose `col` value isn't in `keep` are skipped
    before any parsing; these tables run to millions of rows and we need
    a few thousand, so this is most of the run time. A row for one of our
    filings with the wrong number of fields (a stray tab or line break in
    the source) is recorded in RUN["malformed"] instead of being dropped
    or misaligned."""
    member = next(n for n in zf.namelist() if n.upper().endswith("/" + name + ".TSV"))
    with zf.open(member) as raw:
        text = io.TextIOWrapper(raw, encoding="latin-1", newline="")
        header = [h.strip().upper() for h in next(text).rstrip("\r\n").split("\t")]
        idx = header.index(col) if col else None
        for line in text:
            row = line.rstrip("\r\n").split("\t")
            if idx is not None:
                if len(row) <= idx or row[idx].strip() not in keep:
                    continue
                if len(row) != len(header):
                    RUN["malformed"][name].append(line[:200])
                    continue
                RUN["rows_read"][name] += 1
            elif len(row) < len(header):
                continue
            yield dict(zip(header, (c.strip() for c in row)))


def parse_date(s):
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def money(s, where="", blank_ok=False):
    """Parse an amount. A blank (where blank isn't allowed) or unreadable
    amount is recorded in RUN["bad_amounts"] rather than quietly becoming $0."""
    s = (s or "").strip()
    if not s and blank_ok:
        return 0.0
    try:
        return float(s.replace(",", "").replace("$", ""))
    except ValueError:
        RUN["bad_amounts"].append(f"{where}: {s!r}")
        return 0.0


def zip5(s):
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits[:5] if len(digits) >= 5 else ""


def name_of(r, first, last):
    return " ".join(filter(None, [r.get(first, ""), r.get(last, "")]))


def load_filings(zf, filer_ids):
    """filing_id -> {filer_id, amend_id, form, thru}, keeping only the
    highest amendment of each filing."""
    best = {}
    for r in read_tsv(zf, "CVR_CAMPAIGN_DISCLOSURE_CD", "FILER_ID", filer_ids):
        fid = r.get("FILER_ID", "")
        if fid not in filer_ids:
            continue
        form = r.get("FORM_TYPE", "").upper()
        if form not in ("F460", "F497"):
            continue
        thru = parse_date(r.get("THRU_DATE", ""))
        if fid == BBC_ID and form == "F460" and (not thru or thru < BBC_SINCE):
            continue
        filing = r["FILING_ID"]
        amend = int(r.get("AMEND_ID") or 0)
        if filing not in best or amend > best[filing]["amend_id"]:
            best[filing] = {"filer_id": fid, "amend_id": amend, "form": form, "thru": thru}
    return best


def latest_460(filings):
    """Latest 460 period end per committee: 497s after this date are new money."""
    cutoff = {}
    for f in filings.values():
        if f["form"] == "F460" and f["thru"]:
            cutoff[f["filer_id"]] = max(cutoff.get(f["filer_id"], f["thru"]), f["thru"])
    return cutoff


def current(filings, r, form):
    f = filings.get(r.get("FILING_ID"))
    if not f or f["form"] != form or int(r.get("AMEND_ID") or 0) != f["amend_id"]:
        return None
    return f


def load_receipts(zf, filings, cutoff):
    rows = []
    for r in read_tsv(zf, "RCPT_CD", "FILING_ID", filings):
        f = current(filings, r, "F460")
        if not f or r.get("FORM_TYPE", "").upper() not in ("A", "C"):  # monetary + in-kind
            continue
        rows.append({
            "filer_id": f["filer_id"], "source": "F460", "tran_id": r.get("TRAN_ID", ""),
            "name": name_of(r, "CTRIB_NAMF", "CTRIB_NAML"),
            "entity": r.get("ENTITY_CD", ""), "donor_cmte_id": r.get("CMTE_ID", ""),
            "city": r.get("CTRIB_CITY", ""), "state": r.get("CTRIB_ST", "").upper(),
            "zip": zip5(r.get("CTRIB_ZIP4", "")), "employer": r.get("CTRIB_EMP", ""), "occupation": r.get("CTRIB_OCC", ""),
            "date": parse_date(r.get("RCPT_DATE", "")), "amount": money(r.get("AMOUNT"), f"RCPT_CD filing {r.get('FILING_ID')} {r.get('TRAN_ID')}"),
        })
    return rows


def load_reported_totals(zf, filings):
    """Each committee's own reported totals from its Form 460 summary pages,
    summed over every period (latest amendment of each filing).
    Line 1 monetary, 2 loans, 4 nonmonetary (in-kind), 5 total received.
    Includes unitemized (<$100) money, unlike the itemized schedules."""
    totals = defaultdict(lambda: defaultdict(float))
    for r in read_tsv(zf, "SMRY_CD", "FILING_ID", filings):
        f = current(filings, r, "F460")
        if f and r.get("FORM_TYPE", "").upper() == "F460" and r.get("LINE_ITEM") in ("1", "2", "4", "5"):
            totals[f["filer_id"]][r["LINE_ITEM"]] += money(r.get("AMOUNT_A"), f"SMRY_CD filing {r.get('FILING_ID')} line {r.get('LINE_ITEM')}", blank_ok=True)
    return totals


def load_bbc_spending(zf, filings):
    """Building a Better California's payments: Sched E plus late
    contributions it made after its last 460."""
    rows = []
    for r in read_tsv(zf, "EXPN_CD", "FILING_ID", filings):
        f = current(filings, r, "F460")
        if f and f["filer_id"] == BBC_ID and r.get("FORM_TYPE", "").upper() == "E":
            rows.append({"payee": name_of(r, "PAYEE_NAMF", "PAYEE_NAML"),
                         "payee_cmte_id": r.get("CMTE_ID", ""),
                         "amount": money(r.get("AMOUNT"), f"EXPN_CD filing {r.get('FILING_ID')} {r.get('TRAN_ID')}")})
    return rows


def load_497s(zf, filings, cutoff, receipts, bbc_spending):
    for r in read_tsv(zf, "S497_CD", "FILING_ID", filings):
        f = current(filings, r, "F497")
        if not f:
            continue
        d = parse_date(r.get("CTRIB_DATE", ""))
        if d is None:
            # Without a date we can't tell whether it's already on a 460,
            # so it can't be counted or skipped safely. check_data() stops
            # the run so a person can look at it.
            RUN["undated_late"].append(f"filing {r.get('FILING_ID')} {r.get('TRAN_ID')} "
                                       f"{r.get('ENTY_NAML', '')} {r.get('AMOUNT', '')} "
                                       f"date={r.get('CTRIB_DATE', '')!r}")
            continue
        cut = cutoff.get(f["filer_id"])
        if cut and d <= cut:
            continue  # already on a 460
        part = r.get("FORM_TYPE", "").upper()
        if part == "F497P1":  # received
            receipts.append({
                "filer_id": f["filer_id"], "source": "F497", "tran_id": r.get("TRAN_ID", ""),
                "name": name_of(r, "ENTY_NAMF", "ENTY_NAML"),
                "entity": r.get("ENTITY_CD", ""), "donor_cmte_id": r.get("CMTE_ID", ""),
                "city": r.get("ENTY_CITY", ""), "state": r.get("ENTY_ST", "").upper(),
                "zip": zip5(r.get("ENTY_ZIP4", "")), "employer": r.get("CTRIB_EMP", ""), "occupation": r.get("CTRIB_OCC", ""),
                "date": d, "amount": money(r.get("AMOUNT"), f"S497_CD filing {r.get('FILING_ID')} {r.get('TRAN_ID')}"),
            })
        elif part == "F497P2" and f["filer_id"] == BBC_ID:  # made
            bbc_spending.append({"payee": name_of(r, "ENTY_NAMF", "ENTY_NAML"),
                                 "payee_cmte_id": r.get("CMTE_ID", ""),
                                 "amount": money(r.get("AMOUNT"), f"S497_CD filing {r.get('FILING_ID')} {r.get('TRAN_ID')}")})


def dedupe(rows):
    """Same 497 can be filed twice under different filing IDs. Two rows are
    one contribution only if committee, report type, transaction ID, amount,
    date and contributor name all match; removals are recorded and printed."""
    seen, out = set(), []
    for row in rows:
        key = (row["filer_id"], row["source"], row["tran_id"], row["amount"],
               row["date"], norm(row["name"]))
        if row["tran_id"] and key in seen:
            RUN["duplicates_removed"].append(
                f"{row['filer_id']} {row['source']} {row['tran_id']} {row['date']} "
                f"{row['name']} {row['amount']:,.2f}")
            continue
        seen.add(key)
        out.append(row)
    return out


def check_data(receipts, reported_by_cmte):
    """Print what was read and stop the run, before anything is written or
    published, if the source data has problems we'd otherwise handle
    silently. GitHub emails on a failed run."""
    print("Run checks")
    for t, n in sorted(RUN["rows_read"].items()):
        print(f"  {t}: {n:,} rows read for our committees, "
              f"{len(RUN['malformed'][t])} malformed")
    print(f"  duplicate late reports removed: {len(RUN['duplicates_removed'])}")
    for d in RUN["duplicates_removed"]:
        print(f"    {d}")
    undated_460 = [r for r in receipts if r["source"] == "F460" and r["date"] is None]
    zero = [r for r in receipts if r["amount"] == 0]
    print(f"  undated statement contributions (dates only affect charts): {len(undated_460)}")
    print(f"  zero-dollar contributions: {len(zero)}")

    problems = []
    for t, lines in RUN["malformed"].items():
        if lines:
            problems.append(f"{len(lines)} malformed {t} row(s) for our committees, e.g. {lines[0]!r}")
    if RUN["bad_amounts"]:
        problems.append(f"{len(RUN['bad_amounts'])} unreadable amount(s), e.g. {RUN['bad_amounts'][0]}")
    if RUN["undated_late"]:
        problems.append(f"{len(RUN['undated_late'])} late contribution(s) with no usable date, "
                        f"e.g. {RUN['undated_late'][0]}")
    negative = {f: v for f, v in reported_by_cmte.items() if v < 0}
    if negative:
        problems.append(f"negative reported total(s): {negative}")
    if problems:
        sys.exit("STOPPED, nothing written or published:\n  - " + "\n  - ".join(problems))
    print("  all checks passed")


def load_crosswalk():
    """ZIP (ZCTA) -> (county FIPS, county name) for California, assigning
    ZIPs that cross county lines to the county holding most of their land."""
    best = {}
    with open(CROSSWALK, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh, delimiter="|"):
            z, fips = r["GEOID_ZCTA5_20"], r["GEOID_COUNTY_20"]
            if not z or not fips.startswith("06"):
                continue
            land = int(r["AREALAND_PART"] or 0)
            if z not in best or land > best[z][0]:
                best[z] = (land, fips, r["NAMELSAD_COUNTY_20"].replace(" County", ""))
    return {z: (v[1], v[2]) for z, v in best.items()}


def write_csv(path, header, rows):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def norm(name):
    return " ".join(name.upper().replace(",", " ").replace(".", " ").split())


ENTITY_TYPES = {"IND": "Individual", "COM": "Committee", "RCP": "Committee",
                "SCC": "Committee", "PTY": "Party", "OTH": "Business or group"}
COMMITTEE_NAMES = {
    "1484470": "Yes on 40 - Billionaire Tax Now - No on 41 & 42 (SEIU-UHW)",
    "1495036": "Tax the Ultra-Rich Now, Yes on 3 & 40, No on 41 & 42",
    "1492108": "No on Prop 40 - Doctors, Teachers, Small Businesses...",
    "1486695": "Golden State Promise - No on Prop 40",
    "1485007": "Stop the Squeeze - No on Prop 40",
    "1485228": "Californians Against Tax Increases (CA Business Roundtable)",
    "1488423": "Yes on 41 - Californians for Transparency and Accountability",
    "1488418": "Yes on 42 - Protect Retirement & Life Savings",
    "1497412": "Working Families Say No to Props 41 and 42, Sponsored by Labor Unions",
}


def place(r):
    city = " ".join(w.capitalize() for w in r["city"].split())
    if not r["state"]:
        return "Unknown"
    return f"{city}, {r['state']}" if city else r["state"]


KEEP_UPPER = {"PORAC", "PIC", "AFL", "CIO", "LLC", "LP", "LLP", "CEO", "CFO", "COO", "CTO", "SEIU", "PAC", "USA",
              "II", "III", "IV", "UHW", "VP", "MD", "UC", "AI", "IT", "NA"}
NOT_AN_EMPLOYER = {"", "NONE", "N/A", "NA", "-", "NOT EMPLOYED", "INFORMATION REQUESTED"}


def tidy(s):
    """'CO-FOUNDER' -> 'Co-Founder'; leaves acronyms like LLC and CEO alone.
    Only applied to text filed in all caps."""
    if not s or s != s.upper():
        return s
    small = {"A", "AN", "AND", "OF", "THE", "FOR", "ON", "TO", "IN", "AT", "BY"}

    def word(m):
        w = m.group(0)
        if w in KEEP_UPPER:
            return w
        if w in small and m.start() > 0:
            return w.lower()
        return w.capitalize()
    return re.sub(r"[A-Za-z]+", word, s)


def donor_summary(rows, limit=25):
    """Top donors (merged by normalized name) and top places for a set of
    itemized receipts."""
    donors = {}
    for r in rows:
        k = norm(r["name"])
        d = donors.setdefault(k, {"name": r["name"], "amount": 0.0, "n": 0,
                                  "type": ENTITY_TYPES.get(r["entity"], "Other"),
                                  "_places": defaultdict(float), "_jobs": defaultdict(float)})
        d["amount"] += r["amount"]
        d["n"] += 1
        d["_places"][place(r)] += r["amount"]
        occ = r.get("occupation", "").strip()
        emp = r.get("employer", "").strip()
        emp = "" if emp.upper() in NOT_AN_EMPLOYER else emp
        if occ or emp:
            d["_jobs"][(tidy(occ), tidy(emp))] += abs(r["amount"])
    top = sorted(donors.values(), key=lambda d: -d["amount"])[:limit]
    for d in top:
        d["place"] = max(d.pop("_places").items(), key=lambda kv: kv[1])[0]
        # Occupation and employer as filed on the donor's largest contributions.
        jobs = d.pop("_jobs")
        if jobs:
            d["occupation"], d["employer"] = max(jobs.items(), key=lambda kv: kv[1])[0]
        d["amount"] = round(d["amount"])
        nm = d["name"].upper()
        if "BUILDING A BETTER CALIFORNIA" in nm:
            d["drill"] = "bbc"
        elif SEIU_NAME.search(nm):
            d["note"] = "Union treasury or union committee; its money comes from member dues and isn't itemized further."
    for d in donors.values():
        d.pop("_places", None)
        d.pop("_jobs", None)

    places = defaultdict(float)
    states = defaultdict(float)
    for r in rows:
        places[place(r)] += r["amount"]
        states["California" if r["state"] == "CA" else
               "Unknown" if not r["state"] else "Out of state"] += r["amount"]
    by_type = defaultdict(float)
    for r in rows:
        by_type[ENTITY_TYPES.get(r["entity"], "Other")] += r["amount"]
    return {
        "itemized_total": round(sum(r["amount"] for r in rows)),
        "contributions": len(rows),
        "donor_count": len(donors),
        "top_donors": top,
        "top_places": [{"place": p, "amount": round(v)}
                       for p, v in sorted(places.items(), key=lambda kv: -kv[1])[:12]],
        "by_state": {k: round(v) for k, v in states.items()},
        "by_type": {k: round(v) for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])},
    }


def write_site_data(receipts, reported_by_cmte, bbc, bbc_spending, stats):
    """site/data.json for the interactive faction explorer."""
    seg_of = {f: slice_of(f) for f in POSITIONS}
    segments = []
    for seg_id, label in SLICES:
        ids = {f for f in POSITIONS if seg_of[f] == seg_id}
        if not ids:
            continue  # e.g. no multi-measure committee yet
        rows = [r for r in receipts
                if r["filer_id"] in ids and r["donor_cmte_id"] not in ids]
        segments.append({
            "id": seg_id,
            "label": label,
            "camp": camp(next(iter(ids))),
            "reported_total": round(sum(reported_by_cmte[f] for f in ids)),
            "committees": [{"id": f, "name": COMMITTEE_NAMES[f],
                            "reported_total": round(reported_by_cmte[f])}
                           for f in sorted(ids, key=lambda f: -reported_by_cmte[f])],
            **donor_summary(rows),
        })

    # Where Building a Better California's money went, by faction.
    went = defaultdict(float)
    by_name = [(re.compile(r"\bON (PROP(OSITION)? ?)?40\b"), "no40"),
               (re.compile(r"\bON (PROP(OSITION)? ?)?41\b"), "yes41"),
               (re.compile(r"\bON (PROP(OSITION)? ?)?42\b"), "yes42")]
    for s in bbc_spending:
        seg = seg_of.get(s["payee_cmte_id"])
        if not seg:  # committee ID missing on some records; fall back to the payee name
            seg = next((sid for pat, sid in by_name if pat.search(s["payee"].upper())), "other")
        went[seg] += s["amount"]

    data = {
        "updated": stats["updated"],
        "as_of": stats["as_of"],
        "segments": segments,
        "bbc": {
            "name": "Building a Better California",
            "filer_id": BBC_ID,
            "raised": stats["bbc_raised"],
            "spent": stats["bbc_spent"],
            "spent_to": {k: round(v) for k, v in went.items()},
            **donor_summary(bbc),
        },
    }
    site = HERE / "site"
    site.mkdir(exist_ok=True)
    (site / "data.json").write_text(json.dumps(data, indent=1))

    # Every itemized contribution to these committees, for the search tab.
    # Public Cal-Access fields only: name, city, state, ZIP, employer,
    # occupation. No street addresses.
    rows_out = []
    for r in receipts:
        if r["filer_id"] in seg_of:
            ids = {f for f in POSITIONS if seg_of[f] == seg_of[r["filer_id"]]}
            if r["donor_cmte_id"] in ids:
                continue  # transfer within the faction
            faction, recipient = seg_of[r["filer_id"]], COMMITTEE_NAMES[r["filer_id"]]
        elif r["filer_id"] == BBC_ID:
            faction, recipient = "bbc", "Building a Better California (pass-through committee)"
        else:
            continue
        rows_out.append([
            str(r["date"] or ""), r["name"], ENTITY_TYPES.get(r["entity"], "Other"),
            " ".join(w.capitalize() for w in r["city"].split()), r["state"], r["zip"],
            r["employer"], r["occupation"], faction, recipient, round(r["amount"], 2), r["source"],
        ])
    rows_out.sort(key=lambda x: -x[10])
    (site / "contributions.json").write_text(json.dumps({
        "updated": stats["updated"],
        "as_of": stats["as_of"],
        "columns": ["date", "name", "type", "city", "state", "zip", "employer",
                    "occupation", "faction", "recipient", "amount", "form"],
        "rows": rows_out,
    }, separators=(",", ":")))


def main():
    if not ZIP_PATH.exists():
        sys.exit(f"Missing {ZIP_PATH}. Download dbwebexport.zip first.")
    OUT.mkdir(exist_ok=True)

    with zipfile.ZipFile(ZIP_PATH) as zf:
        # The state builds the snapshot overnight; the filings table's
        # timestamp is the "data as of" date shown to readers.
        cvr = next(i for i in zf.infolist() if i.filename.upper().endswith("/CVR_CAMPAIGN_DISCLOSURE_CD.TSV"))
        as_of = date(*cvr.date_time[:3]).isoformat()
        filings = load_filings(zf, set(POSITIONS) | {BBC_ID})
        cutoff = latest_460(filings)
        receipts = load_receipts(zf, filings, cutoff)
        reported = load_reported_totals(zf, filings)
        bbc_spending = load_bbc_spending(zf, filings)
        load_497s(zf, filings, cutoff, receipts, bbc_spending)
    receipts = dedupe(receipts)
    xwalk = load_crosswalk()

    # Each committee's reported total: Form 460 summary line 5 (all
    # contributions, including unitemized and in-kind) summed over every
    # statement, plus 24-hour reports filed since its last statement.
    late = defaultdict(float)
    for r in receipts:
        if r["source"] == "F497" and r["filer_id"] in POSITIONS:
            late[r["filer_id"]] += r["amount"]
    # Money moved between committees in the same camp would count twice.
    intra = defaultdict(float)
    for r in receipts:
        if (r["filer_id"] in POSITIONS and r["donor_cmte_id"] in POSITIONS
                and camp(r["filer_id"]) == camp(r["donor_cmte_id"])):
            intra[r["filer_id"]] += r["amount"]
    reported_by_cmte = {f: reported[f]["5"] + late[f] - intra[f] for f in POSITIONS}

    # Stop here, before writing anything, if the source data has problems.
    check_data(receipts, reported_by_cmte)

    # Transfers between Prop 40 committees would be counted twice.
    rows = [r for r in receipts
            if r["filer_id"] in COMMITTEES and r["donor_cmte_id"] not in COMMITTEES]
    bbc = [r for r in receipts if r["filer_id"] == BBC_ID]

    for row in rows:
        row["side"], row["committee"] = COMMITTEES[row["filer_id"]]
        cty = xwalk.get(row["zip"]) if row["state"] == "CA" else None
        row["county_fips"], row["county"] = cty if cty else ("", "")

    # Full itemized list, for checking and for your own reporting.
    write_csv(
        OUT / "prop40_contributions.csv",
        ["side", "committee", "filer_id", "source", "date", "name", "entity",
         "employer", "city", "state", "zip", "county", "amount"],
        [[r["side"], r["committee"], r["filer_id"], r["source"], r["date"] or "",
          r["name"], r["entity"], r["employer"], r["city"], r["state"], r["zip"],
          r["county"], f'{r["amount"]:.2f}'] for r in rows],
    )

    # Top donors: one row per donor (merged by normalized name across
    # committees), with separate Yes and No amounts, ranked by their total.
    donors = defaultdict(lambda: {"Yes": 0.0, "No": 0.0})
    display = {}
    for r in rows:
        k = norm(r["name"])
        donors[k][r["side"]] += r["amount"]
        display.setdefault(k, r["name"])
    top = sorted(donors.items(), key=lambda kv: -(kv[1]["Yes"] + kv[1]["No"]))[:20]
    write_csv(
        OUT / "top_donors.csv",
        ["Donor", "Yes on 40", "No on 40"],
        [[display[k], round(v["Yes"]) or "", round(v["No"]) or ""] for k, v in top],
    )

    # In vs. out of state.
    def bucket(r):
        return "California" if r["state"] == "CA" else ("Unknown" if not r["state"] else "Out of state")
    geo = defaultdict(float)
    for r in rows:
        geo[(r["side"], bucket(r))] += r["amount"]
    write_csv(
        OUT / "in_out_state.csv",
        ["Side", "California", "Out of state", "Unknown"],
        [[s] + [round(geo[(s, b)]) for b in ("California", "Out of state", "Unknown")]
         for s in ("Yes", "No")],
    )

    # Maps: individual donors only.
    people = [r for r in rows if r["entity"] == "IND"]
    county_names = {fips: name for fips, name in xwalk.values()}
    by_county = defaultdict(lambda: {"Yes": 0.0, "No": 0.0, "n": 0})
    by_zip = defaultdict(lambda: {"Yes": 0.0, "No": 0.0, "n": 0})
    for r in people:
        if r["county_fips"]:
            for agg in (by_county[r["county_fips"]], by_zip[r["zip"]]):
                agg[r["side"]] += r["amount"]
                agg["n"] += 1
    write_csv(
        OUT / "map_county.csv",
        ["FIPS", "County", "Total", "Yes on 40", "No on 40", "Contributions"],
        # Blank total for counties with no donors, so they show as "no data"
        # rather than sharing the lowest color bin.
        [[f, county_names[f], round(v["Yes"] + v["No"]) or "", round(v["Yes"]), round(v["No"]), v["n"]]
         for f in sorted(county_names) for v in [by_county[f]]],
    )
    write_csv(
        OUT / "map_zip.csv",
        ["ZIP", "County", "Total", "Yes on 40", "No on 40", "Contributions"],
        [[z, xwalk[z][1], round(v["Yes"] + v["No"]), round(v["Yes"]), round(v["No"]), v["n"]]
         for z, v in sorted(by_zip.items())],
    )

    # Building a Better California: who funds it, where its money went.
    bbc_donors, bbc_display = defaultdict(float), {}
    for r in bbc:
        k = norm(r["name"])
        bbc_donors[k] += r["amount"]
        bbc_display.setdefault(k, r["name"])
    bbc_raised = sum(bbc_donors.values())
    bbc_spent = sum(s["amount"] for s in bbc_spending)
    bbc_to_40 = sum(s["amount"] for s in bbc_spending
                    if s["payee_cmte_id"] in COMMITTEES or PROP40_NAME.search(s["payee"].upper()))
    bbc_out_of_state = sum(r["amount"] for r in bbc if r["state"] and r["state"] != "CA")
    # Datawrapper table: top 25 donors with where they're from and what
    # they do, as reported on the filings.
    write_csv(
        OUT / "bbc_donors.csv",
        ["Donor", "Location", "Occupation / employer", "Contributed ($)"],
        [[tidy(d["name"]), d["place"],
          ", ".join(x for x in (d.get("occupation", ""), d.get("employer", "")) if x),
          d["amount"]]
         for d in donor_summary(bbc, limit=25)["top_donors"]],
    )

    # Who pays for each side of Props 40, 41 and 42, and for each camp across
    # all three. A transfer between committees on the same side is dropped so
    # it isn't counted twice.
    def by_funder(committee_ids):
        out = defaultdict(float)
        for r in receipts:
            if r["filer_id"] in committee_ids and r["donor_cmte_id"] not in committee_ids:
                out[funder(r)] += r["amount"]
        return out

    measure_totals = {}
    for m in MEASURES:
        table = []
        for side in ("Yes", "No"):
            ids = {f for f, pos in POSITIONS.items() if pos.get(m) == side}
            money_in = by_funder(ids)
            measure_totals[f"{side.lower()}_{m}"] = round(sum(money_in.values()))
            table.append([f"{side} on {m}"] + [round(money_in[g]) for g in FUNDERS])
        write_csv(OUT / f"funding_{m}.csv", ["Side"] + list(FUNDERS), table)

    # One chart across all three measures: each top donor's itemized money,
    # split by the kind of committee that received it. Committees on the
    # Yes on 40 / No on 41 and 42 side each work on several measures from one
    # account, so their money can't be split by measure; the other side has
    # one committee per measure. Transfers within a slice are excluded.
    recipient_labels = {
        "yes40": "Yes on 40 / No on 41 & 42 (committees covering several props)",
        "multi": "No on 40 / Yes on 41 or 42 (committees covering several props)",
        "no40": "No on 40 (single-prop committees)",
        "yes41": "Yes on 41 (single-prop committee)",
        "yes42": "Yes on 42 (single-prop committee)",
    }
    seg_ids = {sid: {f for f in POSITIONS if slice_of(f) == sid} for sid, _ in SLICES}
    present = [sid for sid, _ in SLICES if seg_ids[sid]]
    by_donor = defaultdict(lambda: defaultdict(float))
    donor_name = {}
    for r in receipts:
        if r["filer_id"] not in POSITIONS:
            continue
        sid = slice_of(r["filer_id"])
        if r["donor_cmte_id"] in seg_ids[sid]:
            continue
        k = norm(r["name"])
        by_donor[k][sid] += r["amount"]
        donor_name.setdefault(k, tidy(r["name"]))
    top_all = sorted(by_donor.items(), key=lambda kv: -sum(kv[1].values()))[:15]
    write_csv(
        OUT / "donors_by_recipient.csv",
        ["Donor"] + [recipient_labels[sid] for sid in present],
        [[donor_name[k]] + [round(v[sid]) or "" for sid in present] for k, v in top_all],
    )


    # One column per slice that has committees; each committee in exactly one.
    parts = [(sid, label) for sid, label in SLICES if any(slice_of(f) == sid for f in POSITIONS)]
    camps = {}
    for c, label in (("pro", "Yes on 40, No on 41 and 42"), ("anti", "No on 40, Yes on 41 and 42")):
        camps[c] = [label] + [
            round(sum(v for f, v in reported_by_cmte.items() if camp(f) == c and slice_of(f) == sid))
            for sid, _ in parts]
    write_csv(OUT / "coalition.csv", ["Camp"] + [label for _, label in parts],
              [camps["pro"], camps["anti"]])
    reported_parts = dict(zip([label for _, label in parts],
                              [a + b for a, b in zip(camps["pro"][1:], camps["anti"][1:])]))

    # Numbers the chart notes quote, so they refresh with the data.
    latest = max((r["date"] for r in rows if r["date"]), default=None)
    totals = defaultdict(float)
    for r in rows:
        totals[r["side"]] += r["amount"]
    stats = {
        "updated": str(latest or ""),   # latest contribution date
        "as_of": as_of,                 # date of the Cal-Access snapshot
        "yes_total": round(totals["Yes"]),
        "no_total": round(totals["No"]),
        "people_yes": sum(1 for r in people if r["side"] == "Yes"),
        "people_no": sum(1 for r in people if r["side"] == "No"),
        "people_share_yes": sum(r["amount"] for r in people if r["side"] == "Yes") / (totals["Yes"] or 1),
        "people_share_no": sum(r["amount"] for r in people if r["side"] == "No") / (totals["No"] or 1),
        "bbc_raised": round(bbc_raised),
        "bbc_spent": round(bbc_spent),
        "bbc_to_40": round(bbc_to_40),
        "bbc_share_to_40": bbc_to_40 / (bbc_spent or 1),
        "bbc_out_of_state_share": bbc_out_of_state / (bbc_raised or 1),
        "pro_total": sum(camps["pro"][1:]),
        "anti_total": sum(camps["anti"][1:]),
        "reported_no_40": reported_parts["No on 40"],
        "intra_camp_transfers": round(sum(intra.values())),
        **measure_totals,
    }
    (OUT / "stats.json").write_text(json.dumps(stats, indent=2))

    write_site_data(receipts, reported_by_cmte, bbc, bbc_spending, stats)

    print(f"{len(rows):,} itemized contributions, latest dated {latest}")
    for s in ("Yes", "No"):
        print(f"  {s}: ${totals[s]:,.0f}")
    by_cmte = defaultdict(float)
    for r in rows:
        by_cmte[r["filer_id"]] += r["amount"]
    for fid, (side, name) in COMMITTEES.items():
        print(f"  {fid} {side:3} ${by_cmte[fid]:>14,.0f}  {name}")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
