#!/usr/bin/env bash
# Reads all notebooks from NotebookLM and creates nlm aliases from their titles.
# Run once after `nlm login`, then re-run whenever you add new notebooks.
set -euo pipefail

echo "Seeding nlm aliases from notebook names..."
echo ""

python3 /dev/stdin <<'PYEOF' <(nlm notebook list)
import json, sys, re, subprocess

notebooks_json = open(sys.argv[1]).read()
data = json.loads(notebooks_json)

skipped = 0
seeded = 0

for nb in data:
    title = (nb.get("title") or "").strip()
    nb_id = nb.get("id", "")
    if not title or not nb_id:
        skipped += 1
        continue

    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")

    result = subprocess.run(
        ["nlm", "alias", "set", slug, nb_id, "--type", "notebook"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        seeded += 1
        print(f"  ✓  {slug}")
        print(f"       {title}")
    else:
        print(f"  ✗  {slug} — {result.stderr.strip()}")

print("")
print(f"Seeded {seeded} aliases, skipped {skipped} notebooks without titles.")
print("Run 'nlm alias list' to verify.")
PYEOF
