#!/bin/bash
# ECCOFS daily RBL4DVAR cycle + forecast. Settings: docs/running.md. Design: TECHNICAL.md.
#
# Runs on the main node via the platform's run-script.sh (see INFRASTRUCTURE.md).
# PUBLISH=production (default) | validation | none picks where products go.

set -eo pipefail
ulimit -s unlimited || true

BASENAME="${0##*/}"
log()        { echo "$(date '+%Y-%m-%d %H:%M:%S') ${BASENAME} - ${1}"; }
error_exit() { ERROR_MSG="${1}"; log "ERROR: ${1}"; exit 1; }

PUBLISH=${PUBLISH:-production}
case "$PUBLISH" in production|validation|none) ;; *) echo "PUBLISH must be production, validation or none" >&2; exit 1 ;; esac
[ -n "${TEST_MODE:-}" ] && PUBLISH=none   # test runs never publish or post to Slack
VALIDATION_PREFIX=${VALIDATION_PREFIX:-pio-validation}

# Mirror output to a file so a failure message can quote its tail.
MAIN_LOG=/tmp/eccofs_run.$$.log
: > "$MAIN_LOG"
exec > >(tee -a "$MAIN_LOG") 2>&1

# Step timing and the optional Slack summary (posted only if SLACK_WEBHOOK_URL is set).
SCRIPT_START=$(date +%s)
STEP_NAME=""; STEP_START=0; STEP_REPORT=""; ERROR_MSG=""

fmt_duration() {  # <seconds> -> "1h 12m 03s"
    local s=$1
    printf '%dh %02dm %02ds' $((s / 3600)) $(((s % 3600) / 60)) $((s % 60))
}

end_step() {
    [ -n "$STEP_NAME" ] || return 0
    STEP_REPORT+="$(printf '%-55s %s' "$STEP_NAME" "$(fmt_duration $(( $(date +%s) - STEP_START )))")"$'\n'
    STEP_NAME=""
}

step() {  # <label>
    end_step
    STEP_NAME=$1; STEP_START=$(date +%s)
    log "$1"
}

send_slack() {  # <message>
    [ -n "${SLACK_WEBHOOK_URL:-}" ] && [ "$PUBLISH" != none ] || return 0
    $PY -c 'import json, sys; print(json.dumps({"text": sys.stdin.read()}))' <<< "$1" \
        | curl -sf -X POST -H "Content-type: application/json" -d @- "$SLACK_WEBHOOK_URL" >/dev/null \
        || log "WARN: Slack notification failed"
}

on_exit() {
    local rc=$? failed="$STEP_NAME" total tail_log
    sleep 0.2   # let tee flush before quoting the log
    end_step
    total=$(fmt_duration $(( $(date +%s) - SCRIPT_START )))
    if [ "$rc" -eq 0 ]; then
        send_slack "$(printf 'ECCOFS run complete: %s\nTotal: %s\n\n%s' "${DATE:-unknown}" "$total" "$STEP_REPORT")"
    else
        tail_log=$(tail -n 25 "$MAIN_LOG" 2>/dev/null | sed '/^[[:space:]]*$/d')
        send_slack "$(printf 'ECCOFS run FAILED: %s\nFailed at: %s (after %s)\nError: %s\n\nLast output:\n%s\n\nSteps:\n%s' \
            "${DATE:-unknown}" "${failed:-setup}" "$total" "${ERROR_MSG:-command failed (rc=$rc)}" "$tail_log" "$STEP_REPORT")"
    fi
    exit "$rc"
}
trap 'ERROR_MSG="${ERROR_MSG:-command failed (line $LINENO): $BASH_COMMAND}"' ERR
trap on_exit EXIT

PY=/opt/conda-eccofs/bin/python
NCRCAT=/opt/conda-eccofs/bin/ncrcat
NCKS=/opt/conda-eccofs/bin/ncks
NCATTED=/opt/conda-eccofs/bin/ncatted
NL_ROMS=/opt/eccofs/bin/romsM_nl_v2
DA_ROMS=/opt/eccofs/bin/romsM_da_v2
FRCT_ROMS=/opt/eccofs/bin/romsM_fwd_pio_v2_sta
for f in $PY $NCRCAT $NCKS $NCATTED $NL_ROMS $DA_ROMS $FRCT_ROMS; do
    [ -x "$f" ] || error_exit "missing executable: $f"
done

MAX_RETRIES=${MAX_RETRIES:-2}
TEST_NTIMES=${TEST_NTIMES:-20}

# Force EFA, no silent TCP fallback.
export OMPI_MCA_pml=${OMPI_MCA_pml:-cm}
export OMPI_MCA_btl=${OMPI_MCA_btl:-self}

RANK_WRAP=/opt/rank-wrap.sh   # lifts each rank's stack limit

# "ROMS: DONE" in the log, not the exit code, is the success signal -- see TECHNICAL.md ("Success check").
# Retries only cover a launch that never happened (empty log).
run_model() {  # <binary> <stdin.in> <logfile>
    local bin=$1 stdin=$2 logf=$3 attempt=1 rc size v fwd
    while :; do
        log "mpirun $(basename "$bin") (attempt ${attempt}/${MAX_RETRIES}): np=$NP ppn=$RANKS_PER_NODE log=$logf"
        fwd=(-x PATH -x LD_LIBRARY_PATH)
        for v in $(compgen -e | grep -E '^FI_'); do fwd+=(-x "$v"); done
        set +e
        mpirun -hostfile "$HOSTFILE" -np "$NP" --npernode "$RANKS_PER_NODE" \
            "${fwd[@]}" "$RANK_WRAP" "$bin" "$stdin" 2>&1 | tee "$logf"
        rc=${PIPESTATUS[0]}
        set -e
        size=$(stat -c %s "$logf" 2>/dev/null || echo 0)
        if [ "$rc" -eq 0 ] && [ "$size" -gt 0 ]; then
            log "$(basename "$bin") finished"
            return 0
        fi
        if grep -q "ROMS: DONE" "$logf" 2>/dev/null; then
            log "$(basename "$bin") exited rc=$rc after 'ROMS: DONE' -- treating as success"
            return 0
        fi
        log "$(basename "$bin") attempt $attempt failed: rc=$rc log=${size}B"
        [ "$attempt" -ge "$MAX_RETRIES" ] && error_exit "$(basename "$bin") failed after ${MAX_RETRIES} attempts (rc=$rc)"
        attempt=$((attempt + 1))
        sleep 60
    done
}

nrec_of() {  # <ncfile> -> record count or 0
    [ -e "$1" ] || { echo 0; return; }
    ncdump -h "$1" | grep UNLIMITED | grep -oE '[0-9]+' | head -1
}

#------------------------------------------------------------------------------
step "STEP 0: Sync static tree from S3"
#------------------------------------------------------------------------------
aws s3 sync s3://fathom-hpc-data/models/eccofs/data/config /models/eccofs/data/config --exact-timestamps --no-progress | tail -1 || true
aws s3 sync s3://fathom-hpc-data/models/eccofs/data/data   /models/eccofs/data/data   --exact-timestamps --no-progress | tail -1 || true
aws s3 sync s3://fathom-hpc-data/models/eccofs/data/forcing/era5 /models/eccofs/data/forcing/era5 --exact-timestamps --no-progress | tail -1 || true
# std_background (365 x ~1GB) stays in S3; STEP 3 fetches the day's two files.

#------------------------------------------------------------------------------
step "STEP 1: Dates and workdir"
#------------------------------------------------------------------------------
if [ -n "${RUN_DATE:-}" ]; then
    DATE=${RUN_DATE}
else
    export TZ="America/New_York"
    DATE=$(date -d "3 days ago" "+%Y%m%d")
fi
DATE1=$(date -d "${DATE} + 1 day" "+%Y%m%d")
DATE2=$(date -d "${DATE} + 2 days" "+%Y%m%d")
DATE3=$(date -d "${DATE} + 3 days" "+%Y%m%d")
PREV_DATE=$(date -d "${DATE} - 1 day" "+%Y%m%d")
DAY=$(date -u -d "${DATE}" +'%s' | awk '{print ($1-1293840000)/86400}')
DAY_OF_YEAR=$(echo "$DAY" | awk '{print int($1 + 1.5 - 365.25 * int($1 / 365.25))}')
[ "$DAY_OF_YEAR" -eq 366 ] && DAY_OF_YEAR=1

EFS_CONFIG=/models/eccofs/data/config
EFS_RESTART=/models/eccofs/runs/${PREV_DATE}/restart
OUT_RESTART=/models/eccofs/runs/${DATE}/restart

# Shared workdir on EFS; templates use /scratch/<date>, a per-node symlink to it -- see TECHNICAL.md ("Storage").
WORK=/models/eccofs/runs/${DATE}/work
SCRATCH=/scratch/${DATE}
OBS_DIR=${SCRATCH}/input/OBS
rm -rf "$WORK"
mkdir -p "$WORK" "$OUT_RESTART"
mpirun -hostfile "$HOSTFILE" --map-by ppr:1:node bash -c \
    "mkdir -p /scratch && { [ -L '$SCRATCH' ] || rm -rf '$SCRATCH'; } && ln -sfn '$WORK' '$SCRATCH'" \
    || error_exit "per-node workdir symlink failed"
mkdir -p "$OBS_DIR" "$SCRATCH/logs"
cd "$SCRATCH"

log "DATE=$DATE (+$DATE1 $DATE2 $DATE3) DAY=$DAY DOY=$DAY_OF_YEAR nodes=$AWS_BATCH_JOB_NUM_NODES np=$NP ${TEST_MODE:+TEST_MODE=$TEST_NTIMES steps}"

#------------------------------------------------------------------------------
step "STEP 2: Stage daily obs and forcing"
#------------------------------------------------------------------------------
RUN_DATE=$DATE timeout 14400 $PY -u ${EFS_CONFIG}/copy_assimilation_mercator_from_s3.py \
    || error_exit "obs staging failed"
OBS_COUNT=$(ls ${OBS_DIR}/*.nc 2>/dev/null | wc -l)
log "OBS files staged: $OBS_COUNT"
[ "$OBS_COUNT" -lt 23 ] && error_exit "expected >=23 OBS files, found $OBS_COUNT"

# Copies for NODD, taken before the format fix below.
NODD_PUB=${SCRATCH}/nodd_pub
mkdir -p "$NODD_PUB"/{frc,obs,riv}
for v in lwrad lwrad_down Pair Qair rain swrad Tair Uwind Vwind; do
    cp -p "${OBS_DIR}/ECCOFS_REALTIME_MERGED_${v}_${DATE}.nc" "$NODD_PUB/frc/eccofs_frc_${v}_${DATE}.nc" \
        || log "WARN: NODD frc copy failed for $v"
done
cp -p "${OBS_DIR}/ssh_sst_cmems_v2_${DATE}.nc" "$NODD_PUB/obs/eccofs_obs_ssh_sst_cmems_v2_${DATE}.nc" \
    || log "WARN: NODD obs copy failed (ssh_sst)"
cp -p "${OBS_DIR}/ssh_sst_cmems_v2_${DATE}_3km.nc" "$NODD_PUB/obs/eccofs_obs_ssh_sst_cmems_v2_${DATE}_3km.nc" \
    || log "WARN: NODD obs copy failed (ssh_sst 3km)"
cp -p "${OBS_DIR}/ECCOFS_splitrivers_file_3km_${DATE}.nc" "$NODD_PUB/riv/eccofs_riv_${DATE}.nc" \
    || log "WARN: NODD riv copy failed"

# Reformat every staged file for PIO -- see TECHNICAL.md ("NetCDF format").
SPLITRIVERS=${OBS_DIR}/ECCOFS_splitrivers_file_3km_${DATE}.nc
if [ -f "$SPLITRIVERS" ]; then
    $NCATTED -O -a rivers,global,d,, "$SPLITRIVERS" \
        || log "WARN: failed to strip 'rivers' attribute from $SPLITRIVERS -- PIO may abort on this file"
    $NCKS -O -x -v river_temp "$SPLITRIVERS" "$SPLITRIVERS.tmp" && mv -f "$SPLITRIVERS.tmp" "$SPLITRIVERS" \
        || log "WARN: failed to strip zero-length river_temp from $SPLITRIVERS -- PIO may abort on this file"
fi
for f in ${OBS_DIR}/*.nc; do
    $NCKS -O --fl_fmt=netcdf4_classic "$f" "$f.tmp" && mv -f "$f.tmp" "$f" \
        || log "WARN: failed to reformat $f to NETCDF4_CLASSIC -- PIO may abort on this file"
done

#------------------------------------------------------------------------------
step "STEP 3: Fetch std files, prior state, build input files"
#------------------------------------------------------------------------------
STD_S3=s3://fathom-hpc-data/models/eccofs/data/forcing/std_background
STDname=${SCRATCH}/eccofs6km_std_mesoscale_19932023_${DAY_OF_YEAR}.nc
STDnameF=${SCRATCH}/eccofs6km_std_b_mesoscale_19932023_${DAY_OF_YEAR}.nc
aws s3 cp ${STD_S3}/$(basename "$STDname")  "$STDname"  --no-progress || error_exit "STD fetch failed"
aws s3 cp ${STD_S3}/$(basename "$STDnameF") "$STDnameF" --no-progress || error_exit "STD_F fetch failed"

OBSname=ssh_sst_cmems_v2_${DATE}.nc
OBSname_3km=ssh_sst_cmems_v2_${DATE}_3km.nc
cp -p ${OBS_DIR}/${OBSname} ${OBS_DIR}/${OBSname_3km} .

INIname=eccofs_ini_${DATE}.nc
RSTname=eccofs_rst_${DATE}.nc
PRIOR=${EFS_RESTART}/eccofs_ini_prior_${DATE}.nc
if [ ! -f "$PRIOR" ]; then
    log "prior not found: $PRIOR — available runs:"
    ls /models/eccofs/runs/ 2>/dev/null || true
    error_exit "missing prior initial conditions"
fi
cp -p "$PRIOR" "$INIname"

# Copy each template, then fill in the day's placeholders (see docs/operations.md).
gen() { cp "${EFS_CONFIG}/$1" "$2"; }
sub() { sed -i "s|$2|$3|g" "$1"; }

gen roms_nl_eccofs.in roms_nl_eccofs.in
sub roms_nl_eccofs.in MyDSTART "${DAY}.0d0"
sub roms_nl_eccofs.in MyAPARNAM is4dvar_3km.in
sub roms_nl_eccofs.in MyCYCLE 0
sub roms_nl_eccofs.in MyDate "$DATE"

gen roms_da_eccofs.in roms_da_eccofs.in
sub roms_da_eccofs.in MyDSTART "${DAY}.0d0"
sub roms_da_eccofs.in MyAPARNAM is4dvar.in
sub roms_da_eccofs.in MyDate "$DATE"

gen s4dvar_3km.in is4dvar_3km.in
sub is4dvar_3km.in roms_obs.nc "$OBSname_3km"
sub is4dvar_3km.in roms_mod.nc "eccofs_mod_${DATE}.nc"
sub is4dvar_3km.in MyOuterLoop 0
sub is4dvar_3km.in MyPhase4DVAR background

gen s4dvar.in is4dvar.in
sub is4dvar.in roms_std_i.nc "$STDname"
sub is4dvar.in roms_std_m.nc "$STDname"
sub is4dvar.in roms_std_f.nc "$STDnameF"
sub is4dvar.in roms_obs.nc "$OBSname"
sub is4dvar.in roms_mod.nc "eccofs_mod_${DATE}.nc"
sub is4dvar.in MyOuterLoop 1
sub is4dvar.in MyPhase4DVAR increment

gen roms_nl_eccofs_forecast.in roms_nl_eccofs_forecast.in
sub roms_nl_eccofs_forecast.in MyDSTART "${DAY}.0d0"
sub roms_nl_eccofs_forecast.in MyDate "$DATE"
sub roms_nl_eccofs_forecast.in stations_adcp.in eccofs_stations_20260722.in
# the forecast template reads these from the current directory
cp -p ${EFS_CONFIG}/varinfo.yaml ${EFS_CONFIG}/eccofs_stations_20260722.in .

# Retiles for the actual node count -- see TECHNICAL.md ("Tiling").
NTILEJ=12
[ $(( NP % NTILEJ )) -eq 0 ] || error_exit "NP=$NP not divisible by NTILEJ=$NTILEJ -- cannot retile domain"
NTILEI=$(( NP / NTILEJ ))
log "domain tiling: NtileI=${NTILEI} NtileJ=${NTILEJ} (NP=$NP)"
for f in roms_nl_eccofs.in roms_da_eccofs.in roms_nl_eccofs_forecast.in; do
    sub "$f" "NtileI == 16" "NtileI == ${NTILEI}"
    sub "$f" "NtileJ == 12" "NtileJ == ${NTILEJ}"
done

NREC_FULL=37
if [ -n "${TEST_MODE:-}" ]; then
    log "TEST_MODE: NTIMES/output intervals -> $TEST_NTIMES; NREC gates relaxed"
    for f in roms_nl_eccofs.in roms_da_eccofs.in roms_nl_eccofs_forecast.in; do
        sed -i -E "s#^([[:space:]]*(NTIMES|NTIMES_ANA|NTIMES_FCT|NRST|NHIS|NQCK|NAVG)[[:space:]]*==?[[:space:]]*)[0-9]+#\1${TEST_NTIMES}#" "$f"
    done
    NREC_FULL=1
fi

#------------------------------------------------------------------------------
step "STEP 4: Outer loop 0 — NL forward (background)"
#------------------------------------------------------------------------------
run_model "$NL_ROMS" roms_nl_eccofs.in "$SCRATCH/logs/log0"
cp -p "$RSTname" "eccofs_rst_${DATE}_outer0.nc" 2>/dev/null || log "WARN: no $RSTname after outer0"
cp -p "eccofs6km_fwd_${DATE}_outer0.nc" "eccofs6km_fwd_${DATE}_0.nc" 2>/dev/null || true
for i in 0002 0003 0004; do mv -f "eccofs_fwd_${DATE}_outer0_${i}.nc" "eccofs_fwd_${DATE}_0_${i}.nc" 2>/dev/null || true; done
cp -p "eccofs_mod_${DATE}.nc" "eccofs_mod_${DATE}_0.nc" 2>/dev/null || true

cycle=1
NREC0=$(nrec_of "eccofs6km_fwd_${DATE}_0.nc"); NREC1=0; NREC2=0
log "outer0 NREC=$NREC0 (need >=$NREC_FULL)"
[ "${NREC0:-0}" -lt "$NREC_FULL" ] && cycle=3

#------------------------------------------------------------------------------
step "STEP 5: Outer loops 1-2 — DA increment + coarse2fine + NL"
#------------------------------------------------------------------------------
while [ "$cycle" -lt 3 ]; do
    log "--- outer loop $cycle"
    if [ "$cycle" -eq 1 ]; then
        $NCRCAT -d ocean_time,0,0 -v zeta,ubar,vbar,u,v,temp,salt,AKv,AKt,AKs "eccofs6km_fwd_${DATE}_0.nc" "eccofs6km_ini_${DATE}.nc"
        $NCRCAT "eccofs6km_ini_${DATE}.nc" "eccofs6km_ini_${DATE}.nc" -O "eccofs6km_ini_${DATE}.nc"
    fi

    run_model "$DA_ROMS" roms_da_eccofs.in "$SCRATCH/logs/log${cycle}.2"

    attempt=1
    while :; do
        PYTHONPATH=${EFS_CONFIG} $PY ${EFS_CONFIG}/coarse2fine_ini0.py --day="${DATE}" --output "$SCRATCH/logs/log${cycle}.3" && break
        [ "$attempt" -ge "$MAX_RETRIES" ] && error_exit "coarse2fine_ini0.py failed after $MAX_RETRIES attempts"
        attempt=$((attempt + 1)); log "coarse2fine retry in 60s"; sleep 60
    done

    run_model "$NL_ROMS" roms_nl_eccofs.in "$SCRATCH/logs/log${cycle}.4"

    cp -p "$RSTname" "eccofs_rst_${DATE}_outer${cycle}.nc" 2>/dev/null || true
    cp -p "eccofs6km_fwd_${DATE}_outer0.nc" "eccofs6km_fwd_${DATE}_${cycle}.nc" 2>/dev/null || true
    for i in 0001 0002 0003 0004; do mv -f "eccofs_fwd_${DATE}_outer0_${i}.nc" "eccofs_fwd_${DATE}_${cycle}_${i}.nc" 2>/dev/null || true; done
    cp -p "eccofs_mod_${DATE}.nc" "eccofs_mod_${DATE}_${cycle}.nc" 2>/dev/null || true

    NREC=$(nrec_of "eccofs6km_fwd_${DATE}_${cycle}.nc")
    log "outer$cycle NREC=$NREC (need >=$NREC_FULL)"
    if [ "${NREC:-0}" -lt "$NREC_FULL" ]; then
        cycle=3
    else
        [ "$cycle" -eq 1 ] && NREC1=$NREC || NREC2=$NREC
    fi
    cycle=$((cycle + 1))
done

#------------------------------------------------------------------------------
step "STEP 6: Next-day priors -> EFS $OUT_RESTART"
#------------------------------------------------------------------------------
cat "$SCRATCH"/logs/log* > "$SCRATCH/logs/da_log.${DATE}" 2>/dev/null || true

make_priors() {  # <loop-suffix>
    $NCRCAT -v zeta,ubar,vbar,u,v,temp,salt,AKv,AKt,AKs -h -O "eccofs_fwd_${DATE}_$1_0002.nc" "$OUT_RESTART/eccofs_ini_prior_${DATE1}.nc" && \
    $NCRCAT -v zeta,ubar,vbar,u,v,temp,salt,AKv,AKt,AKs -h -O "eccofs_fwd_${DATE}_$1_0003.nc" "$OUT_RESTART/eccofs_ini_prior_${DATE2}.nc" && \
    $NCRCAT -v zeta,ubar,vbar,u,v,temp,salt,AKv,AKt,AKs -h -O "eccofs_fwd_${DATE}_$1_0004.nc" "$OUT_RESTART/eccofs_ini_prior_${DATE3}.nc"
}
if [ -n "${TEST_MODE:-}" ] && [ ! -e "eccofs_fwd_${DATE}_2_0002.nc" ] && [ ! -e "eccofs_fwd_${DATE}_1_0002.nc" ] && [ ! -e "eccofs_fwd_${DATE}_0_0002.nc" ]; then
    log "TEST_MODE: split fwd files absent (short run) — skipping prior generation"
elif [ "$NREC2" -ge "$NREC_FULL" ] && [ -e "eccofs_fwd_${DATE}_2_0002.nc" ]; then
    make_priors 2 || error_exit "prior generation (loop 2) failed"
elif [ "$NREC1" -ge "$NREC_FULL" ] && [ -e "eccofs_fwd_${DATE}_1_0002.nc" ]; then
    log "loop-2 incomplete — using loop-1 solution"
    mv -f "eccofs6km_fwd_${DATE}_1.nc" "eccofs6km_fwd_${DATE}_2.nc" 2>/dev/null || true
    make_priors 1 || error_exit "prior generation (loop 1) failed"
elif [ "${NREC0:-0}" -ge "$NREC_FULL" ] && [ -e "eccofs_fwd_${DATE}_0_0002.nc" ]; then
    log "no assimilation completed — using forward run without assimilation"
    make_priors 0 || error_exit "prior generation (loop 0) failed"
else
    error_exit "no outer loop completed with $NREC_FULL records — cannot generate priors"
fi

#------------------------------------------------------------------------------
step "STEP 7: Forecast (romsM_fwd_pio_v2_sta)"
#------------------------------------------------------------------------------
run_model "$FRCT_ROMS" roms_nl_eccofs_forecast.in "$SCRATCH/logs/nl_log.${DATE}"

#------------------------------------------------------------------------------
step "STEP 8: Extract qck/sta nowcast products"
#------------------------------------------------------------------------------
QCKname=eccofs_qck_${DATE}.nc
STAname=eccofs_sta_${DATE}.nc
QCKfwd=eccofs_qck_${DATE}_fwd.nc
QCKfwd4=eccofs_qck_${DATE}_fwd.nc4
STAnowcast=eccofs_sta_${DATE}_nowcast.nc

# Records 0-23 are the nowcast day; the fwd product keeps hour 24 onward.
# The quicksave may be split into daily parts.
qck_parts=( eccofs_qck_${DATE}_[0-9]*.nc )
if [ -e "$QCKname" ]; then
    ( $NCKS -d ocean_time,24, "$QCKname" "$QCKfwd" && $NCKS -4 "$QCKfwd" "$QCKfwd4" ) \
        || log "WARN: qck fwd extraction failed (short run?)"
elif [ -e "${qck_parts[0]}" ]; then
    ( $NCRCAT -O -d ocean_time,24, "${qck_parts[@]}" "$QCKfwd" && $NCKS -4 "$QCKfwd" "$QCKfwd4" ) \
        || log "WARN: qck fwd extraction failed (short run?)"
else
    log "WARN: no qck output found -- skipping qck extraction"
fi
if [ -e "$STAname" ]; then
    $NCKS -d ocean_time,72,95 "$STAname" "$STAnowcast" \
        || log "WARN: sta nowcast extraction failed (short run?)"
else
    log "WARN: $STAname not found -- skipping sta extraction"
fi

#------------------------------------------------------------------------------
step "STEP 9: MLD/OHC augmentation"
#------------------------------------------------------------------------------
# A failure here warns instead of aborting a completed science run.
avg_files=( eccofs_avg*.nc )
if [ -e "${avg_files[0]}" ]; then
    PYTHONPATH=${EFS_CONFIG} $PY ${EFS_CONFIG}/append_mld_ohc.py "${avg_files[@]}" \
        || log "WARN: append_mld_ohc.py failed -- avg files lack MLD/OHC"
else
    log "WARN: no eccofs_avg*.nc files -- skipping MLD/OHC"
fi

#------------------------------------------------------------------------------
step "STEP 10: Publish (PUBLISH=$PUBLISH)"
#------------------------------------------------------------------------------
# Every upload warns on failure rather than aborting, so a publish problem never loses the run.
NODD_BUCKET=s3://noaa-nos-eccofs-pds
NODD_SECRET_ID=${NODD_SECRET_ID:-eccofs-hpc/nodd-credentials}

publish_production() {
    local secret creds d
    secret=$(aws secretsmanager get-secret-value --secret-id "$NODD_SECRET_ID" \
        --query SecretString --output text 2>/dev/null) || secret=""
    creds=$($PY -c '
import json, sys
d = json.load(sys.stdin)
print("AWS_ACCESS_KEY_ID=%s AWS_SECRET_ACCESS_KEY=%s" % (d["AWS_ACCESS_KEY_ID"], d["AWS_SECRET_ACCESS_KEY"]))
' <<< "$secret" 2>/dev/null) || creds=""
    if [ -n "$creds" ]; then
        # <S3 dir>:<include glob>:<exclude glob>
        local spec
        for spec in 'avg:eccofs_avg*.nc:' 'qck:eccofs_qck*.nc:*_fwd.nc' 'his:eccofs_his*.nc:' 'sta:eccofs_sta*.nc:*_nowcast.nc'; do
            IFS=: read -r d inc exc <<< "$spec"
            env $creds aws s3 cp . "${NODD_BUCKET}/$d/" --recursive --exclude "*" --include "$inc" \
                ${exc:+--exclude "$exc"} --no-progress || log "WARN: NODD $d upload failed"
        done
        for d in frc obs riv; do
            env $creds aws s3 cp "$NODD_PUB/$d/" "${NODD_BUCKET}/$d/" --recursive --no-progress \
                || log "WARN: NODD $d upload failed"
        done
    else
        log "WARN: no NODD credentials from $NODD_SECRET_ID -- skipping NODD publish"
    fi

    # Versioned icechunk/zarr archive at s3://fathom-eccofs/archive/ice.
    if [ -e "${avg_files[0]}" ]; then
        $PY ${EFS_CONFIG}/upload_eccofs_s3.py "${DATE}" "${avg_files[@]}" || log "WARN: archive upload failed"
    fi
}

publish_validation() {  # same products under their own prefix, so production output is never overwritten
    local dest=s3://fathom-eccofs/${VALIDATION_PREFIX}/${DATE}
    [ -e "$QCKfwd4" ]    && { aws s3 cp "$QCKfwd4"    "$dest/qck_files/"   --no-progress || log "WARN: qck upload failed"; }
    [ -e "$STAnowcast" ] && { aws s3 cp "$STAnowcast" "$dest/sta_nowcast/" --no-progress || log "WARN: sta nowcast upload failed"; }
    [ -e "$STAname" ]    && { aws s3 cp "$STAname"    "$dest/stations/"    --no-progress || log "WARN: sta upload failed"; }
    [ -e "$QCKfwd" ]     && { aws s3 cp "$QCKfwd"     "$dest/outputs/"     --no-progress || log "WARN: qck fwd upload failed"; }
    local pat
    for pat in eccofs_avg*.nc eccofs_his*.nc; do
        aws s3 cp . "$dest/outputs/" --recursive --exclude "*" --include "$pat" --no-progress \
            || log "WARN: $pat upload failed"
    done
    log "validation products: $dest (compare with the same date under s3://fathom-eccofs/{qck_files,sta_nowcast,stations,outputs}/)"
}

case "$PUBLISH" in
    production) publish_production ;;
    validation) publish_validation ;;
    none)       log "publishing off; products stay in $SCRATCH (KEEP_WORKDIR=1 to inspect them)" ;;
esac

#------------------------------------------------------------------------------
step "STEP 11: Manifest and cleanup"
#------------------------------------------------------------------------------
log "restart chain (the only per-run data kept):"
ls -lh "$OUT_RESTART"/ 2>/dev/null | awk 'NR>1 {print "  " $5 "  " $NF}'

if [ -n "${KEEP_WORKDIR:-}" ]; then
    log "KEEP_WORKDIR set -- leaving $WORK in place"
else
    cd /
    rm -rf "$WORK"
fi

# Old run dirs are working state; everything worth keeping is on S3.
KEEP_RUNS=${KEEP_RUNS:-3}
RUNS_ROOT=/models/eccofs/runs
mapfile -t ALL_RUNS < <(ls -1 "$RUNS_ROOT" 2>/dev/null | sort -r)
for old in "${ALL_RUNS[@]:$KEEP_RUNS}"; do
    log "pruning old run dir: $RUNS_ROOT/$old"
    rm -rf "${RUNS_ROOT:?}/${old}"
done

log "ECCOFS RUN COMPLETE (date=$DATE, PUBLISH=$PUBLISH${TEST_MODE:+, TEST_MODE})"
