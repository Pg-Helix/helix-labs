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
# Restricted to exactly the three repos this script actually checks out
# (see the note below the table in MANIFEST.md) -- the unrestricted
# '^\| helix-' pattern used here previously also matched the
# helix-atlas/helix-protocol/helix-infer/helix-labs rows, the last of
# which resolves to this very repo's own directory (git fetch/checkout
# against a commit column that isn't even a real SHA for that row) --
# a real bug, caught by actually running this end to end rather than
# only reading it.
grep -E '^\| (helix-foundry|helix-kernel|helix-cortex) ' "$MANIFEST" | while IFS='|' read -r _ repo ref commit _; do
    repo=$(echo "$repo" | xargs)
    # Strip markdown backticks around the SHA in the table (`abc123`) --
    # left in place, git fetch/checkout receive a literal backtick-
    # wrapped string and fail with "couldn't find remote ref `abc123`".
    commit=$(echo "$commit" | xargs | tr -d '`')
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
