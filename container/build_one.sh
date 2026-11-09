#!/bin/bash
# Builds one ECCOFS ROMS binary: build_one.sh <ROMS_APPLICATION> <header.h> <output name>
# Uses ROMS_EXTRA_CPPDEFS from the environment -- see TECHNICAL.md ("Three compiles").
set -eo pipefail
app=$1; header=$2; out=$3

. /etc/profile.d/hpc-20-roms.sh
dir=$(mktemp -d); cd "$dir"
cp /opt/roms/build_roms.sh "/opt/roms/$header" .
sed -i "s|^export[[:space:]]*ROMS_APPLICATION=.*|export ROMS_APPLICATION=$app|" build_roms.sh
sed -i "s|^export MY_CPP_FLAGS=\$|export MY_CPP_FLAGS=\"${ROMS_EXTRA_CPPDEFS}\"|" build_roms.sh
# Fail the build if the extra flags did not land.
[ -z "$ROMS_EXTRA_CPPDEFS" ] || grep -qF "MY_CPP_FLAGS=\"$ROMS_EXTRA_CPPDEFS\"" build_roms.sh

./build_roms.sh -j "$(nproc)" > build.log 2>&1 || { tail -60 build.log; exit 1; }
cp romsM "/opt/eccofs/bin/$out"
cd / && rm -rf "$dir"
