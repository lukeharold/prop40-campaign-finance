#!/bin/zsh
# Daily Prop 40 refresh: download Cal-Access export, rebuild CSVs, push to
# Datawrapper. Run by launchd (see com.lukeharold.prop40.plist); logs to
# logs/refresh.log.

set -euo pipefail
cd "$(dirname "$0")"
mkdir -p raw logs

# launchd doesn't load ~/.zshrc, so fall back to a token file.
if [[ -z "${DATAWRAPPER_TOKEN:-}" && -f "$HOME/.datawrapper_token" ]]; then
  export DATAWRAPPER_TOKEN="$(<"$HOME/.datawrapper_token")"
fi

echo "=== $(date) ==="

# Download to a temp file so a failed download never clobbers the last good one.
curl -sSL --fail --retry 3 -o raw/dbwebexport.zip.part \
  https://campaignfinance.cdn.sos.ca.gov/dbwebexport.zip
mv raw/dbwebexport.zip.part raw/dbwebexport.zip

/usr/bin/env python3 build_data.py
/usr/bin/env python3 push_datawrapper.py

echo "done $(date)"
