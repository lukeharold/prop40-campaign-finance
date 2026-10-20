"""One-off check: for the pass-through committees funding Prop 40, how much
did they raise, from whom, and where did their money go?

Writes output/trace_receipts.csv and output/trace_spending.csv and prints a
summary. Covers filings for periods ending 2025-01-01 or later.
"""

import re
import sys
import zipfile
from collections import defaultdict
from datetime import date

from build_data import COMMITTEES, OUT, ZIP_PATH, money, parse_date, read_tsv, write_csv, zip5

SINCE = date(2025, 1, 1)
PATTERNS = {
    "Building a Better California": re.compile(r"BUILDING A BETTER CALIFORNIA"),
    "SEIU-UHW committee": re.compile(r"(SEIU|SERVICE EMPLOYEES).*UNITED ?HEALTH ?CARE"),
}


def main():
    with zipfile.ZipFile(ZIP_PATH) as zf:
        # 1. Find the committees and their latest-amendment filings.
        names, filings = {}, {}
        for r in read_tsv(zf, "CVR_CAMPAIGN_DISCLOSURE_CD"):
            nm = " ".join([r.get("FILER_NAML", ""), r.get("FILER_NAMF", "")]).upper()
            group = next((g for g, p in PATTERNS.items() if p.search(nm)), None)
            if not group:
                continue
            form = r.get("FORM_TYPE", "").upper()
            thru = parse_date(r.get("THRU_DATE", ""))
            if form not in ("F460", "F497") or (form == "F460" and (not thru or thru < SINCE)):
                continue
            fid = r["FILER_ID"]
            names[fid] = (group, nm.strip())
            amend = int(r.get("AMEND_ID") or 0)
            f = r["FILING_ID"]
            if f not in filings or amend > filings[f]["amend_id"]:
                filings[f] = {"filer_id": fid, "amend_id": amend, "form": form, "thru": thru}

        print("Committees found:")
        for fid, (g, nm) in sorted(names.items()):
            n = sum(1 for f in filings.values() if f["filer_id"] == fid)
            print(f"  {fid}  [{g}] {nm}  ({n} filings)")

        cutoff = {}
        for f in filings.values():
            if f["form"] == "F460" and f["thru"]:
                cutoff[f["filer_id"]] = max(cutoff.get(f["filer_id"], f["thru"]), f["thru"])

        def current(r, form):
            f = filings.get(r.get("FILING_ID"))
            if not f or f["form"] != form or int(r.get("AMEND_ID") or 0) != f["amend_id"]:
                return None
            return f

        receipts, spending = [], []

        # 2. Money in (460 Sched A/C) and money out (460 Sched E).
        for r in read_tsv(zf, "RCPT_CD"):
            f = current(r, "F460")
            if f and r.get("FORM_TYPE", "").upper() in ("A", "C"):
                receipts.append([f["filer_id"], "F460", parse_date(r.get("RCPT_DATE", "")),
                                 " ".join(filter(None, [r.get("CTRIB_NAMF"), r.get("CTRIB_NAML")])),
                                 r.get("ENTITY_CD", ""), r.get("CTRIB_CITY", ""),
                                 r.get("CTRIB_ST", "").upper(), zip5(r.get("CTRIB_ZIP4", "")),
                                 money(r.get("AMOUNT", "0"))])
        for r in read_tsv(zf, "EXPN_CD"):
            f = current(r, "F460")
            if f and r.get("FORM_TYPE", "").upper() == "E":
                spending.append([f["filer_id"], "F460", parse_date(r.get("EXPN_DATE", "")),
                                 " ".join(filter(None, [r.get("PAYEE_NAMF"), r.get("PAYEE_NAML")])),
                                 r.get("CMTE_ID", ""), r.get("EXPN_CODE", ""),
                                 r.get("EXPN_DSCR", ""), money(r.get("AMOUNT", "0"))])

        # 3. Late money after the last 460 (497 part 1 = received, part 2 = made).
        for r in read_tsv(zf, "S497_CD"):
            f = current(r, "F497")
            if not f:
                continue
            d = parse_date(r.get("CTRIB_DATE", ""))
            cut = cutoff.get(f["filer_id"])
            if cut and d and d <= cut:
                continue
            nm = " ".join(filter(None, [r.get("ENTY_NAMF"), r.get("ENTY_NAML")]))
            amt = money(r.get("AMOUNT", "0"))
            part = r.get("FORM_TYPE", "").upper()
            if part == "F497P1":
                receipts.append([f["filer_id"], "F497", d, nm, r.get("ENTITY_CD", ""),
                                 r.get("ENTY_CITY", ""), r.get("ENTY_ST", "").upper(),
                                 zip5(r.get("ENTY_ZIP4", "")), amt])
            elif part == "F497P2":
                spending.append([f["filer_id"], "F497", d, nm, r.get("CMTE_ID", ""),
                                 "CTB", "Late contribution made", amt])

    for row in receipts + spending:
        row[2] = row[2] or ""
    write_csv(OUT / "trace_receipts.csv",
              ["filer_id", "source", "date", "name", "entity", "city", "state", "zip", "amount"],
              receipts)
    write_csv(OUT / "trace_spending.csv",
              ["filer_id", "source", "date", "payee", "payee_cmte_id", "code", "description", "amount"],
              spending)

    prop40_ids = set(COMMITTEES)
    for fid, (g, nm) in sorted(names.items()):
        rin = [r for r in receipts if r[0] == fid]
        rout = [r for r in spending if r[0] == fid]
        tin, tout = sum(r[-1] for r in rin), sum(r[-1] for r in rout)
        to40 = sum(r[-1] for r in rout if r[4] in prop40_ids
                   or re.search(r"\b(PROP(OSITION)?\.? ?40|ON 40)\b", r[3].upper()))
        if not rin and not rout:
            continue
        print(f"\n=== {fid} {nm}")
        print(f"  Raised (itemized): ${tin:,.0f} from {len(rin)} contributions")
        print(f"  Spent (itemized):  ${tout:,.0f}")
        print(f"  To Prop 40 committees: ${to40:,.0f}"
              + (f"  ({to40 / tout:.0%} of spending)" if tout else ""))
        print("  Top donors:")
        agg = defaultdict(float)
        for r in rin:
            agg[(r[3].upper(), r[6], r[7])] += r[-1]
        for (n, st, z), v in sorted(agg.items(), key=lambda kv: -kv[1])[:10]:
            print(f"    ${v:>14,.0f}  {n[:50]}  {st} {z}")
        print("  Top recipients/payees:")
        agg = defaultdict(float)
        for r in rout:
            agg[r[3].upper()] += r[-1]
        for n, v in sorted(agg.items(), key=lambda kv: -kv[1])[:10]:
            print(f"    ${v:>14,.0f}  {n[:60]}")


if __name__ == "__main__":
    sys.exit(main())
