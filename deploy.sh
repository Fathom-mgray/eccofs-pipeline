#!/bin/bash

# Uploads scripts, helpers, config and the manifest to S3 -- see TECHNICAL.md ("Deploying").
# Usage: ./deploy.sh [--dryrun]    (needs the aws CLI; S3_BUCKET defaults to fathom-hpc-data)

set -euo pipefail

BUCKET=${S3_BUCKET:-fathom-hpc-data}
DRYRUN=()
case "${1:-}" in
    "")       ;;
    --dryrun) DRYRUN=(--dryrun) ;;
    *)        echo "usage: $0 [--dryrun]" >&2; exit 1 ;;
esac

cd "$(dirname "$0")"
DEST=s3://$BUCKET/models/eccofs

# No --delete: the S3 config prefix can hold files that are not in this repo.
sync() {  # <local dir> <s3 prefix> <include globs...>
    local src=$1 dst=$2; shift 2
    local inc=()
    for g in "$@"; do inc+=(--include "$g"); done
    echo "$src/ -> $dst/"
    aws s3 sync "$src" "$dst" --exact-timestamps --exclude "*" "${inc[@]}" ${DRYRUN[@]+"${DRYRUN[@]}"}
}

sync scripts "$DEST/jobs"        "*.sh"
sync python  "$DEST/data/config" "*.py"
sync config  "$DEST/data/config" "*.in" "*.yaml"

echo "data.manifest.yaml -> $DEST/data.manifest.yaml"
aws s3 cp data.manifest.yaml "$DEST/data.manifest.yaml" --only-show-errors ${DRYRUN[@]+"${DRYRUN[@]}"}

echo "Done. Config reaches EFS at the next run's STEP 0, or now via: hpc data sync eccofs"
