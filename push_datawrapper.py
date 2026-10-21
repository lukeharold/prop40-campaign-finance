"""Create (first run) or update (every later run) the Prop 40 Datawrapper
charts from the CSVs in output/, then republish them.

Needs DATAWRAPPER_TOKEN in the environment (chart read + write scopes).
Chart IDs are saved to datawrapper_charts.json so embed links never change.

    python3 push_datawrapper.py              # update + publish
    python3 push_datawrapper.py --basemaps   # list California basemaps
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "output"
IDS_FILE = HERE / "datawrapper_charts.json"
API = "https://api.datawrapper.de/v3"

YES, NO = "#1b7f79", "#d9822b"  # teal / orange: deliberately non-partisan

SOURCE = "California Secretary of State, Cal-Access"
SOURCE_URL = ("https://www.sos.ca.gov/campaign-lobbying/helpful-resources/"
              "measure-contributions/2026-ballot-measure-contribution-totals")


def usd(n):
    n = float(n)
    return f"${n / 1e6:,.1f} million" if n >= 1e6 else f"${n:,.0f}"


def pct(x):
    return f"{x:.0%}"


def as_of(s):
    """'Oct. 20, 2026' from the snapshot date (AP-style month)."""
    from datetime import date
    d = date.fromisoformat(s.get("as_of") or s["updated"])
    mon = ["Jan.", "Feb.", "March", "April", "May", "June", "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec."][d.month - 1]
    return f"Data as of {mon} {d.day}, {d.year}, from the state's daily Cal-Access snapshot."


def notes(s):
    """Chart notes, filled from output/stats.json so they stay current."""
    base = ("Itemized contributions ($100+) to the six committees formed for or against "
            "Prop 40, from Form 460s and 24-hour Form 497 reports. " + as_of(s) + " "
            "Both Yes committees also campaign on other measures.")
    bbc = ("Most No money comes through Building a Better California, which raised "
           f"{usd(s['bbc_raised'])} and gave {usd(s['bbc_to_40'])} "
           f"({pct(s['bbc_share_to_40'])} of its spending) to No on 40.")
    return {
        "top_donors": f"{base} {bbc}",
        "in_out_state": (f"{base} Counts direct donors only. {bbc} "
                         f"{pct(s['bbc_out_of_state_share'])} of that committee's money came "
                         "from donors with out-of-state addresses."),
        "bbc_donors": (f"Itemized contributions to Building a Better California since Jan. 1, "
                       f"2025. {as_of(s)} The committee spent {usd(s['bbc_spent'])}, "
                       f"{pct(s['bbc_share_to_40'])} of it on No on 40; the rest went mainly to "
                       "Props 41 and 42 and other measures. Amounts shown are total gifts to "
                       "the committee, not gifts to the Prop 40 campaign."),
        "map": (f"Individual donors only, by home ZIP code: {s['people_yes']} contributions to "
                f"the Yes side ({pct(s['people_share_yes'])} of its money) and "
                f"{s['people_no']} to the No side ({pct(s['people_share_no'])} of its money). "
                "Most money on both sides comes from unions, companies and committees, which "
                f"are not mapped. {bbc} {as_of(s)}"),
        **{f"funding_{m}": funding_note(s) for m in ("40", "41", "42")},
        "coalition": ("Totals are each committee's own reported contributions: the totals on its "
                      "Form 460 campaign statements (including in-kind and contributions under $100), "
                      "plus contributions on 24-hour reports filed since its last statement. "
                      f"{as_of(s)} The Yes on 40 / No on 41 and 42 side is "
                      "SEIU-UHW's \"Yes on 40 - Billionaire Tax Now - No on 41 & 42,\" Tax the "
                      "Ultra-Rich Now, and Working Families Say No to Props 41 and 42, a labor "
                      "committee formed in October that has filed only 24-hour reports so far. "
                      "Props 41 and 42 are separate "
                      "measures to limit new taxes; money for them is not all spent against Prop 40. "
                      "Contributions raised, not money spent."),
    }


def funding_note(s):
    return ("Itemized contributions ($100+) from Form 460s and 24-hour Form 497 reports. "
            f"{as_of(s)} The main Yes on 40 committee, SEIU-UHW's \"Yes on 40 - "
            "Billionaire Tax Now - No on 41 & 42,\" also campaigns against Props 41 and 42, so "
            "its money is counted on each of those sides; the Secretary of State lists it only "
            "under Prop 40. SEIU-UHW includes the union's nonprofit arm and political committees.")


# Stepped color scale: one $5M donor would otherwise wash out every other place.
MAP_COLORSCALE = {
    "enabled": True,
    "mode": "discrete",
    "stops": "custom",
    "stopCount": 5,
    "customStops": [None, 1000, 10000, 100000, 1000000, None],
    "colors": [{"color": "#e3f1ef", "position": 0},
               {"color": YES, "position": 0.6},
               {"color": "#0b3d3a", "position": 1}],
    "interpolation": "equidistant",
}

FUNDER_VIZ = {
    "custom-colors": {"Building a Better California": NO,
                      "SEIU-UHW and its committees": YES,
                      "All other donors": "#bbbbbb"},
    "value-label-format": "$0.[0]a",
    "show-color-key": True,
}

TOOLTIP_BODY = ("Total: ${{ FORMAT(total, \"0,0\") }}<br>"
                "Yes on 40: ${{ FORMAT(yes_on_40, \"0,0\") }}<br>"
                "No on 40: ${{ FORMAT(no_on_40, \"0,0\") }}<br>"
                "Contributions: {{ contributions }}")

CHARTS = {
    "top_donors": {
        "csv": "top_donors.csv",
        "type": "d3-bars-stacked",
        "title": "The biggest donors in the Prop 40 fight",
        "intro": "Largest direct contributors to committees supporting or opposing the billionaire tax",
        "visualize": {
            "custom-colors": {"Yes on 40": YES, "No on 40": NO},
            "value-label-format": "$0.[0]a",
            "show-color-key": True,
        },
    },
    "in_out_state": {
        "csv": "in_out_state.csv",
        "type": "d3-bars-stacked",
        "title": "Where Prop 40 money comes from",
        "intro": "Share of itemized contributions from donors inside and outside California",
        "visualize": {
            "stack-percentages": True,
            "custom-colors": {"California": YES, "Out of state": NO, "Unknown": "#bbbbbb"},
            "show-color-key": True,
        },
    },
    "bbc_donors": {
        "csv": "bbc_donors.csv",
        "type": "tables",
        "title": "Who funds the main committee behind No on 40",
        "intro": "The 25 largest donors to Building a Better California, the committee that supplies most of the No campaign's money. Search or sort by any column.",
        # Datawrapper table: sortable, searchable, with a bar in the amount column.
        "visualize": {
            "searchable": True,
            "sortTable": True,
            "sortBy": "Contributed ($)",
            "sortDirection": "desc",
            "perPage": 10,
            "pagination": {"enabled": True, "position": "bottom"},
            "showHeader": True,
            "striped": False,
            "compactMode": False,
            "firstRowIsHeader": False,
            "columns": {
                # Datawrapper tables ignore a "$" prefix set through the API,
                # so the header carries the unit instead.
                "Contributed ($)": {
                    "showAsBar": True,
                    "barColor": NO,
                    "number-format": "0,0",
                    "alignment": "right",
                },
                "Location": {"alignment": "left"},
                "Occupation / employer": {"alignment": "left"},
            },
        },
    },
    "coalition": {
        "csv": "coalition.csv",
        "type": "d3-bars-stacked",
        "title": "The billionaire tax fight, across three ballot measures",
        "intro": "Total contributions reported by the committees on each side of Props 40, 41 and 42",
        "visualize": {
            "custom-colors": {"Yes on 40 / No on 41 and 42": YES,
                              "Multiple measures: No on 40, Yes on 41 or 42": "#8f4a1c",
                              "No on 40": NO,
                              "Yes on 41": "#e8a96b",
                              "Yes on 42": "#f3cfa6"},
            "value-label-format": "$0.[0]a",
            "show-color-key": True,
        },
    },
    "funding_40": {
        "csv": "funding_40.csv",
        "type": "d3-bars-stacked",
        "title": "Who's paying for each side of Prop 40",
        "intro": "Prop 40 would impose a one-time 5% tax on California residents and trusts worth $1 billion or more",
        "visualize": FUNDER_VIZ,
    },
    "funding_41": {
        "csv": "funding_41.csv",
        "type": "d3-bars-stacked",
        "title": "Who's paying for each side of Prop 41",
        "intro": "Prop 41 would require audits of programs funded by new state special taxes, among other changes",
        "visualize": FUNDER_VIZ,
    },
    "funding_42": {
        "csv": "funding_42.csv",
        "type": "d3-bars-stacked",
        "title": "Who's paying for each side of Prop 42",
        "intro": "Prop 42 would prohibit new state personal property taxes and certain retroactive state taxes",
        "visualize": FUNDER_VIZ,
    },
    "map_county": {
        "csv": "map_county.csv",
        "type": "d3-maps-choropleth",
        "title": "Where individual Prop 40 donors live, by county",
        "intro": "Contributions from individual California donors to both sides",
        "basemap": "california-counties",
        "key_attr": "GEOID",  # 5-digit FIPS, e.g. 06075
        "key_column": "FIPS",
        "value_column": "Total",
        "tooltip_title": "{{ county }} County",
    },
    "map_zip": {
        "csv": "map_zip.csv",
        "type": "d3-maps-choropleth",
        "title": "Where individual Prop 40 donors live, by ZIP code",
        "intro": "Contributions from individual California donors to both sides",
        "basemap": "california-zcta-2020",  # matches the 2020 Census crosswalk
        "key_attr": "GEOID20",
        "key_column": "ZIP",
        "value_column": "Total",
        "tooltip_title": "ZIP {{ zip }} ({{ county }} County)",
    },
}


def token():
    t = os.environ.get("DATAWRAPPER_TOKEN")
    token_file = Path.home() / ".datawrapper_token"
    if not t and token_file.exists():
        t = token_file.read_text().strip()
    if not t:
        sys.exit("No Datawrapper token: set DATAWRAPPER_TOKEN or create ~/.datawrapper_token.")
    return t


def call(method, path, body=None, content_type="application/json"):
    data = None
    if body is not None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token()}",
        "Content-Type": content_type,
    })
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        sys.exit(f"Datawrapper {method} {path} failed: {e.code} {e.read().decode()[:500]}")


def basemaps():
    res = call("GET", "/basemaps")
    return res if isinstance(res, list) else res.get("list", res.get("data", []))


def main():
    if "--basemaps" in sys.argv:
        for b in basemaps():
            if "california" in json.dumps(b).lower():
                print(json.dumps(b)[:200])
        return

    ids = json.loads(IDS_FILE.read_text()) if IDS_FILE.exists() else {}
    stats = json.loads((OUT / "stats.json").read_text())
    chart_notes = notes(stats)

    for name, spec in CHARTS.items():
        csv_text = (OUT / spec["csv"]).read_text()
        is_map = "basemap" in spec

        if name not in ids:
            created = call("POST", "/charts", {"title": spec["title"], "type": spec["type"]})
            ids[name] = created["id"]
            IDS_FILE.write_text(json.dumps(ids, indent=2))
            print(f"created {name}: {ids[name]}")
        cid = ids[name]

        call("PUT", f"/charts/{cid}/data", csv_text, "text/csv")

        meta = {
            "describe": {
                "intro": spec["intro"],
                "source-name": SOURCE,
                "source-url": SOURCE_URL,
                "byline": "Luke Harold",
            },
            "annotate": {"notes": chart_notes["map" if is_map else name]},
            "visualize": dict(spec.get("visualize", {})),
        }
        # Datawrapper converts custom-colors into color-category on first
        # save, and the converted copy then wins, so set both.
        if "custom-colors" in meta["visualize"]:
            meta["visualize"]["color-category"] = {"map": meta["visualize"]["custom-colors"]}
        if is_map:
            meta["axes"] = {"keys": spec["key_column"], "values": spec["value_column"]}
            # Keep leading zeros on FIPS codes (06075, not 6075).
            meta["data"] = {"column-format": {spec["key_column"]: {"type": "text"}}}
            meta["visualize"].update({
                "basemap": spec["basemap"],
                "map-key-attr": spec["key_attr"],
                "colorscale": MAP_COLORSCALE,
                "legends": {"color": {"labelFormat": "$0a"}},
                "tooltip": {"title": spec["tooltip_title"], "body": TOOLTIP_BODY, "enabled": True},
            })
        # Sending the type too lets a chart's type be changed later.
        call("PATCH", f"/charts/{cid}", {"title": spec["title"], "type": spec["type"], "metadata": meta})
        call("POST", f"/charts/{cid}/publish")
        print(f"published {name}: https://datawrapper.dwcdn.net/{cid}/")

    IDS_FILE.write_text(json.dumps(ids, indent=2))


if __name__ == "__main__":
    main()
