#!/bin/sh
# Clones/pins the sibling checkouts the workflow scripts build from
# (helix-foundry, helix-kernel, helix-cortex next to this repo) at the
# commits in MANIFEST.md's "Effective pins" section -- the single
# source both a clean local reproduction and CI (.github/workflows/
# ci.yml) resolve, so the two can never test different stacks.
#
# Detaches each sibling at its pinned commit. Meant for a disposable
# directory of clones; it refuses to touch a sibling with uncommitted
# changes. Works with linked worktrees (.git as a file) too.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PARENT=$(CDPATH= cd -- "$HERE/.." && pwd)

# Rows of the "Effective pins" table only: | repo | `sha` |
pins=$(awk '/^## Effective pins/{on=1; next} /^## /{on=0} on && /^\| helix-/' "$HERE/MANIFEST.md" |
    awk -F'|' '{gsub(/[ `]/, "", $2); gsub(/[ `]/, "", $3); print $2, $3}')
[ "$(echo "$pins" | wc -l)" -eq 3 ] || { echo "prepare-checkouts.sh: expected 3 effective pins in MANIFEST.md, got: $pins" >&2; exit 1; }

echo "$pins" | while read -r repo commit; do
    echo "$commit" | grep -Eq '^[0-9a-f]{40}$' || { echo "prepare-checkouts.sh: bad commit for $repo: $commit" >&2; exit 1; }
    dir="$PARENT/$repo"
    if ! git -C "$dir" rev-parse --git-dir >/dev/null 2>&1; then
        echo "prepare-checkouts.sh: cloning $repo"
        git clone -q "https://github.com/Pg-Helix/$repo.git" "$dir"
    elif [ -n "$(git -C "$dir" status --porcelain --untracked-files=no)" ]; then
        echo "prepare-checkouts.sh: $dir has uncommitted changes; refusing to detach it" >&2
        exit 1
    fi
    git -C "$dir" fetch -q origin "$commit"
    git -C "$dir" checkout -q --detach "$commit"
    echo "prepare-checkouts.sh: $repo @ $(git -C "$dir" rev-parse HEAD)"
done
