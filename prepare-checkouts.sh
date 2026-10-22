#!/bin/sh
# Creates and pins the sibling checkouts workflow-1's scripts assume
# (helix-foundry, helix-kernel, helix-cortex, helix-infer, helix-protocol
# next to this repo), at the exact commits recorded in MANIFEST.md.
# Safe to re-run: fetches and checks out the pinned commit in an
# existing checkout rather than re-cloning. Run from a clean clone of
# this repo to reproduce the acceptance gate exactly.
set -eu
MANIFEST="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/MANIFEST.md"
PARENT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

# Parses MANIFEST.md's "| repo | ref | commit |" table rows.
grep -E '^\| helix-' "$MANIFEST" | while IFS='|' read -r _ repo ref commit _; do
    repo=$(echo "$repo" | xargs)
    commit=$(echo "$commit" | xargs)
    dir="$PARENT/$repo"
    if [ ! -d "$dir/.git" ]; then
        echo "prepare-checkouts.sh: cloning $repo"
        git clone "https://github.com/Pg-Helix/$repo.git" "$dir"
    fi
    echo "prepare-checkouts.sh: pinning $repo to $commit"
    git -C "$dir" fetch origin "$commit"
    git -C "$dir" checkout --detach "$commit"
done

echo "prepare-checkouts.sh: done -- sibling checkouts pinned per MANIFEST.md"
