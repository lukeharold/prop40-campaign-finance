# The billionaire tax fight: Props 40, 41 and 42

Campaign finance for California's November 2026 billionaire tax measure
(Prop 40) and the two linked measures (Props 41 and 42), built from the
Secretary of State's Cal-Access raw data export.

- `site/` – the interactive page (pie chart, faction details, contribution search).
  `data.json` and `contributions.json` are generated; don't edit them by hand.
- `build_data.py` – downloads nothing; reads `raw/dbwebexport.zip` and writes
  `output/` (CSVs for the Datawrapper charts) and the two `site/` data files.
- `push_datawrapper.py` – updates and republishes the Datawrapper charts
  listed in `datawrapper_charts.json`. Needs `DATAWRAPPER_TOKEN`.
- `refresh.sh` – download, build, push, in that order.
- `combine_calaccess_exports.R` – combines a committee's Cal-Access web
  exports (Contributions Received + Late and $5000+) without double counting.
- `ref/zcta_county.txt` – Census 2020 ZIP-to-county relationship file, for the maps.

## Data

Source: [Cal-Access raw data](https://www.sos.ca.gov/campaign-lobbying/helpful-resources/raw-data-campaign-finance-and-lobbying-activity)
(`https://campaignfinance.cdn.sos.ca.gov/dbwebexport.zip`), rebuilt by the state once a day.

Committee totals are each committee's own reported contributions: Form 460
summary totals plus Form 497 late contributions dated after its last
statement. Donor lists use itemized contributions ($100+). Committees are
listed in `POSITIONS` in `build_data.py`; new committees must be added there.

Reporting by Luke Harold.
