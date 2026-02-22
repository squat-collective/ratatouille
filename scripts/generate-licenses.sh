#!/usr/bin/env bash
# Generate third-party license notices for all RAT packages.
# Produces per-package reports in each component directory,
# a combined THIRD-PARTY-NOTICES.md at the repo root,
# and a structured licenses.json for the website.
#
# Usage: ./scripts/generate-licenses.sh
#
# Requirements (pre-installed in scripts/Dockerfile.licenses):
#   - go-licenses (Go)
#   - pip-licenses (Python)
#   - license-checker (Node.js)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_DIR="${REPO_ROOT}/licenses"
mkdir -p "$OUTPUT_DIR"

header() {
  echo ""
  echo "================================================================"
  echo "  $1"
  echo "================================================================"
  echo ""
}

# ── Go (platform/) ──────────────────────────────────────────────
generate_go_licenses() {
  header "Go (platform/) — go-licenses"

  cd "$REPO_ROOT/platform"

  if ! command -v go-licenses &>/dev/null; then
    echo "Installing go-licenses..."
    go install github.com/google/go-licenses@latest
  fi

  # Go doesn't distinguish runtime/dev in go.mod — filter known test-only deps
  local test_deps="testify"

  # Generate full CSV: module,url,license
  # --ignore own module to avoid "no LICENSE found" errors for internal packages
  local go_module
  go_module=$(grep '^module ' go.mod | awk '{print $2}')
  go-licenses report ./... --ignore "${go_module}" 2>/dev/null | sort > "$OUTPUT_DIR/platform-raw.csv" || true

  # Split into runtime and dev
  > "$OUTPUT_DIR/platform-runtime.csv"
  > "$OUTPUT_DIR/platform-dev.csv"

  while IFS=',' read -r mod url license; do
    [ -z "$mod" ] && continue
    if echo "$mod" | grep -qiE "$test_deps"; then
      echo "${mod},${url},${license}" >> "$OUTPUT_DIR/platform-dev.csv"
    else
      echo "${mod},${url},${license}" >> "$OUTPUT_DIR/platform-runtime.csv"
    fi
  done < "$OUTPUT_DIR/platform-raw.csv"

  # Build per-component markdown
  {
    echo "# Third-Party Licenses — platform (ratd)"
    echo ""
    echo "> Auto-generated — do not edit manually."
    echo ""
    echo "## Runtime Dependencies"
    echo ""
    echo "| Module | License | URL |"
    echo "|--------|---------|-----|"
    while IFS=',' read -r mod url license; do
      [ -z "$mod" ] && continue
      if [ -n "$url" ] && [ "$url" != " " ]; then
        echo "| \`${mod}\` | ${license} | ${url} |"
      else
        echo "| \`${mod}\` | ${license} | — |"
      fi
    done < "$OUTPUT_DIR/platform-runtime.csv"
    echo ""
    echo "## Development Dependencies"
    echo ""
    echo "| Module | License | URL |"
    echo "|--------|---------|-----|"
    if [ -s "$OUTPUT_DIR/platform-dev.csv" ]; then
      while IFS=',' read -r mod url license; do
        [ -z "$mod" ] && continue
        if [ -n "$url" ] && [ "$url" != " " ]; then
          echo "| \`${mod}\` | ${license} | ${url} |"
        else
          echo "| \`${mod}\` | ${license} | — |"
        fi
      done < "$OUTPUT_DIR/platform-dev.csv"
    else
      echo "| _none_ | | |"
    fi
  } > "$OUTPUT_DIR/platform.md"

  cp "$OUTPUT_DIR/platform.md" "$REPO_ROOT/platform/THIRD-PARTY-NOTICES.md"
  echo "  -> platform/THIRD-PARTY-NOTICES.md"
}

# ── Python (runner/, query/) ────────────────────────────────────
generate_python_licenses() {
  local pkg_name="$1"
  local pkg_dir="$2"

  header "Python (${pkg_dir}/) — pip-licenses"

  cd "$REPO_ROOT/$pkg_dir"

  if ! command -v pip-licenses &>/dev/null; then
    echo "Installing pip-licenses..."
    pip install --quiet pip-licenses
  fi

  # Install base deps first, capture package list to temp file
  pip install --quiet -e . 2>/dev/null || true
  pip-licenses --format=json --with-urls 2>/dev/null > "$OUTPUT_DIR/${pkg_dir}-runtime-raw.json" || echo "[]" > "$OUTPUT_DIR/${pkg_dir}-runtime-raw.json"

  # Install dev deps, capture full package list to temp file
  pip install --quiet -e ".[dev]" 2>/dev/null || true
  pip-licenses --format=json --with-urls 2>/dev/null > "$OUTPUT_DIR/${pkg_dir}-all-raw.json" || echo "[]" > "$OUTPUT_DIR/${pkg_dir}-all-raw.json"

  # Use Python to diff runtime vs dev and generate markdown + JSON
  python3 -c "
import json, sys

with open('$OUTPUT_DIR/${pkg_dir}-runtime-raw.json') as f:
    runtime_raw = json.load(f)
with open('$OUTPUT_DIR/${pkg_dir}-all-raw.json') as f:
    all_raw = json.load(f)

# Known tool packages to skip
skip = {'pip', 'setuptools', 'wheel', 'pip-licenses', 'prettytable', 'wcwidth',
        '${pkg_name}', 'rat-runner', 'rat-query', 'hatchling', 'hatch-vcs',
        'pathspec', 'trove-classifiers', 'editables'}
runtime_names = {p['Name'].lower() for p in runtime_raw}

runtime = []
dev = []
for pkg in sorted(all_raw, key=lambda x: x['Name'].lower()):
    name = pkg['Name']
    if name.lower() in skip:
        continue
    entry = {
        'name': name,
        'version': pkg.get('Version', ''),
        'license': pkg.get('License', 'Unknown'),
        'url': pkg.get('URL', pkg.get('Home-page', ''))
    }
    if name.lower() in runtime_names:
        runtime.append(entry)
    else:
        dev.append(entry)

# Write JSON for combiner
with open('$OUTPUT_DIR/${pkg_dir}.json', 'w') as f:
    json.dump({'runtime': runtime, 'dev': dev}, f, indent=2)

# Write markdown
with open('$OUTPUT_DIR/${pkg_dir}.md', 'w') as f:
    f.write('# Third-Party Licenses — ${pkg_name}\n\n')
    f.write('> Auto-generated — do not edit manually.\n\n')
    f.write('## Runtime Dependencies\n\n')
    f.write('| Package | Version | License | URL |\n')
    f.write('|---------|---------|---------|-----|\n')
    for p in runtime:
        url = p['url'] if p['url'] else chr(8212)
        f.write(f'| \`{p[\"name\"]}\` | {p[\"version\"]} | {p[\"license\"]} | {url} |\n')
    f.write('\n## Development Dependencies\n\n')
    f.write('| Package | Version | License | URL |\n')
    f.write('|---------|---------|---------|-----|\n')
    if dev:
        for p in dev:
            url = p['url'] if p['url'] else chr(8212)
            f.write(f'| \`{p[\"name\"]}\` | {p[\"version\"]} | {p[\"license\"]} | {url} |\n')
    else:
        f.write('| _none_ | | | |\n')
" || {
    echo "# Third-Party Licenses — ${pkg_name}" > "$OUTPUT_DIR/${pkg_dir}.md"
    echo "" >> "$OUTPUT_DIR/${pkg_dir}.md"
    echo "Error generating license report." >> "$OUTPUT_DIR/${pkg_dir}.md"
    echo '{"runtime":[],"dev":[]}' > "$OUTPUT_DIR/${pkg_dir}.json"
  }

  cp "$OUTPUT_DIR/${pkg_dir}.md" "$REPO_ROOT/${pkg_dir}/THIRD-PARTY-NOTICES.md"
  echo "  -> ${pkg_dir}/THIRD-PARTY-NOTICES.md"
}

# ── Node.js (portal/, sdk-typescript/, website/) ─────────────────
generate_node_licenses() {
  local pkg_name="$1"
  local pkg_dir="$2"

  header "Node.js (${pkg_dir}/) — license-checker"

  cd "$REPO_ROOT/$pkg_dir"

  if ! command -v license-checker &>/dev/null; then
    echo "Installing license-checker..."
    npm install -g license-checker 2>/dev/null
  fi

  # Install deps
  npm ci --silent 2>/dev/null || npm install --silent 2>/dev/null || true

  # Runtime (production) deps — write to temp file
  license-checker --json --production 2>/dev/null > "$OUTPUT_DIR/${pkg_dir}-runtime-raw.json" || echo "{}" > "$OUTPUT_DIR/${pkg_dir}-runtime-raw.json"

  # All deps (includes dev) — write to temp file
  license-checker --json 2>/dev/null > "$OUTPUT_DIR/${pkg_dir}-all-raw.json" || echo "{}" > "$OUTPUT_DIR/${pkg_dir}-all-raw.json"

  # Use Python to diff and generate output — reads from files, not variables
  python3 -c "
import json, sys

with open('$OUTPUT_DIR/${pkg_dir}-runtime-raw.json') as f:
    runtime_raw = json.load(f)
with open('$OUTPUT_DIR/${pkg_dir}-all-raw.json') as f:
    all_raw = json.load(f)

runtime_keys = set(runtime_raw.keys())

runtime = []
dev = []
for key in sorted(all_raw.keys(), key=str.lower):
    info = all_raw[key]
    # Skip the package itself
    if '${pkg_name}'.lower() in key.lower():
        continue
    entry = {
        'name': key,
        'version': '',
        'license': info.get('licenses', 'Unknown'),
        'url': info.get('repository', info.get('url', ''))
    }
    # Extract version from key (format: name@version)
    if '@' in key and not key.startswith('@') or key.count('@') > 1:
        at_idx = key.rfind('@')
        entry['name'] = key[:at_idx]
        entry['version'] = key[at_idx+1:]
    if key in runtime_keys:
        runtime.append(entry)
    else:
        dev.append(entry)

# Write JSON
with open('$OUTPUT_DIR/${pkg_dir}.json', 'w') as f:
    json.dump({'runtime': runtime, 'dev': dev}, f, indent=2)

# Write markdown
with open('$OUTPUT_DIR/${pkg_dir}.md', 'w') as f:
    f.write('# Third-Party Licenses — ${pkg_name}\n\n')
    f.write('> Auto-generated — do not edit manually.\n\n')
    f.write('## Runtime Dependencies\n\n')
    f.write('| Package | Version | License | URL |\n')
    f.write('|---------|---------|---------|-----|\n')
    if runtime:
        for p in runtime:
            url = p['url'] if p['url'] else chr(8212)
            f.write(f'| \`{p[\"name\"]}\` | {p[\"version\"]} | {p[\"license\"]} | {url} |\n')
    else:
        f.write('| _none_ | | | |\n')
    f.write('\n## Development Dependencies\n\n')
    f.write('| Package | Version | License | URL |\n')
    f.write('|---------|---------|---------|-----|\n')
    if dev:
        for p in dev:
            url = p['url'] if p['url'] else chr(8212)
            f.write(f'| \`{p[\"name\"]}\` | {p[\"version\"]} | {p[\"license\"]} | {url} |\n')
    else:
        f.write('| _none_ | | | |\n')
" || {
    echo "# Third-Party Licenses — ${pkg_name}" > "$OUTPUT_DIR/${pkg_dir}.md"
    echo "" >> "$OUTPUT_DIR/${pkg_dir}.md"
    echo "Error generating license report." >> "$OUTPUT_DIR/${pkg_dir}.md"
    echo '{"runtime":[],"dev":[]}' > "$OUTPUT_DIR/${pkg_dir}.json"
  }

  cp "$OUTPUT_DIR/${pkg_dir}.md" "$REPO_ROOT/${pkg_dir}/THIRD-PARTY-NOTICES.md"
  echo "  -> ${pkg_dir}/THIRD-PARTY-NOTICES.md"
}

# ── Combine into root THIRD-PARTY-NOTICES.md + JSON ─────────────
combine_notices() {
  header "Combining into THIRD-PARTY-NOTICES.md + licenses.json"

  # Generate JSON for website (Go doesn't produce JSON natively, convert CSV)
  python3 "$REPO_ROOT/scripts/combine-licenses.py" \
    --output-dir "$OUTPUT_DIR" \
    --repo-root "$REPO_ROOT"

  echo "  -> THIRD-PARTY-NOTICES.md (root)"
  echo "  -> website/data/licenses.json"
}

# ── Main ────────────────────────────────────────────────────────
main() {
  echo "Generating third-party license reports for RAT..."

  generate_go_licenses
  generate_python_licenses "rat-runner" "runner"
  generate_python_licenses "rat-query" "query"
  generate_node_licenses "rat-portal" "portal"
  generate_node_licenses "@rat/client" "sdk-typescript"
  generate_node_licenses "rat-docs" "website"
  combine_notices

  echo ""
  echo "Done! Reports in:"
  echo "  - THIRD-PARTY-NOTICES.md (combined)"
  echo "  - platform/THIRD-PARTY-NOTICES.md"
  echo "  - runner/THIRD-PARTY-NOTICES.md"
  echo "  - query/THIRD-PARTY-NOTICES.md"
  echo "  - portal/THIRD-PARTY-NOTICES.md"
  echo "  - sdk-typescript/THIRD-PARTY-NOTICES.md"
  echo "  - website/THIRD-PARTY-NOTICES.md"
  echo "  - website/data/licenses.json"
}

main "$@"
