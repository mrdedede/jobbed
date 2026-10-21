#!/usr/bin/env bash
# Fails if single-tenant / Streamlit / SQLite leftovers reappear.
# Usage: scripts/check_clean.sh [dir]   (default: repo root)
cd "${1:-$(dirname "$0")/..}" || exit 2

# Allowed to mention the legacy world: the importer, its test, migrations,
# captured fixtures, and this gate. user_info/ holds the user's own
# (gitignored) files, not code. Filtered by path (not grep --exclude, whose
# include/exclude ordering differs between GNU and BSD grep).
ALLOWED='^\./(user_info/|tests/fixtures/|alembic/versions/|\.git/|.*\.egg-info/|scripts/(import_legacy_sqlite|check_clean)\.|tests/test_import_legacy\.)'
INCLUDE=(--include='*.py' --include='*.toml' --include='*.yml'
         --include='*.yaml' --include='*.txt' --include='*.cfg'
         --include='*.sh')

hits=$(
  {
    grep -rniE "streamlit|sqlite|jobbed\.db|PHASE4_PLAN|REFACTORING\.md|claude cli" "${INCLUDE[@]}" .
    grep -rnwE "KEYWORDS_TXT|BLACKLIST_TXT|CV_MD|DB_PATH" "${INCLUDE[@]}" .
  } | grep -vE "$ALLOWED"
)

[ -z "$hits" ] || { echo "$hits"; exit 1; }
