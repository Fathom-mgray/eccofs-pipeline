# Operations

Changing configuration and recovering a broken daily chain. For submitting runs
see [`running.md`](running.md).

## Changing configuration

| Repo path | What |
|---|---|
| `config/roms_nl_eccofs.in`, `roms_da_eccofs.in`, `roms_nl_eccofs_forecast.in` | ROMS input templates (3 km forward, 6 km DA, forecast) |
| `config/s4dvar.in`, `s4dvar_3km.in` | 4D-Var parameters for the 6 km and 3 km passes (they differ in error-covariance files) |
| `config/varinfo.yaml` | ROMS variable metadata |
| `config/eccofs_stations_20260722.in` | station locations for the forecast `sta` output |
| `python/*.py` | helpers the run script calls |

The run script (STEP 3) fills in these placeholders; keep them intact when
editing: `MyDate`, `MyDSTART`, `MyAPARNAM`, `MyCYCLE`, `MyOuterLoop`,
`MyPhase4DVAR`, `roms_obs.nc`, `roms_mod.nc`, `roms_std_{i,m,f}.nc`, and
`NtileI == 16` / `NtileJ == 12` (retiled for the node count).

Merge to `main` and CI runs [`deploy.sh`](../deploy.sh), which uploads to
`s3://fathom-hpc-data/models/eccofs/`. To test before merging, run
`./deploy.sh --dryrun`, then `./deploy.sh`. Each run copies that S3 prefix to
EFS at STEP 0, so the next run uses the change.

## Recovering the daily chain

Each run starts from `/models/eccofs/runs/<date-1>/restart/eccofs_ini_prior_<date>.nc`,
written by the previous day's run. If a day fails or is skipped, every later day
stops at STEP 3 with `missing prior initial conditions`. To recover, pause the
schedule, run the missing days oldest first, then resume.

**1. Find the gap.** Compare the model date of each run (`createdAt` in
America/New_York, minus 3 days) against the last success. Make sure nothing for
those dates is already running.

```bash
aws scheduler get-schedule --name eccofs-hpc-daily-eval \
  --group-name eccofs-hpc-schedules --region us-east-2 --query State
aws batch list-jobs --region us-east-2 --job-queue eccofs-hpc-queue --job-status FAILED
aws batch list-jobs --region us-east-2 --job-queue eccofs-hpc-queue --job-status SUCCEEDED
```

**2. Pause the schedule.** Otherwise it submits a second copy of today's run.
This rewrites the live schedule with only `State` changed:

```bash
set_schedule() {  # ENABLED | DISABLED
  aws scheduler get-schedule --name eccofs-hpc-daily-eval \
    --group-name eccofs-hpc-schedules --region us-east-2 \
    | jq --arg s "$1" '{Name, GroupName, ScheduleExpression, ScheduleExpressionTimezone,
                        FlexibleTimeWindow, Target, Description, State: $s}' > schedule.json
  aws scheduler update-schedule --region us-east-2 --cli-input-json file://schedule.json
}
set_schedule DISABLED
```

**3. Submit the missing days in order.** Same shape as the schedule (4 nodes,
`hpc6a.48xlarge`, 16 h). Each day after the first depends on the one before, so
Batch holds it until the earlier day succeeds and cancels it if that day fails.

```bash
submit() {  # <YYYYMMDD> [previous job id]
  aws batch submit-job --region us-east-2 \
    --job-name "eccofs-hpc-daily-eval-$1" \
    --job-queue eccofs-hpc-queue --job-definition eccofs \
    --node-overrides '{"numNodes":4,"nodePropertyOverrides":[{"targetNodes":"0:",
      "containerOverrides":{"instanceType":"hpc6a.48xlarge",
      "command":["models/eccofs/jobs/run_eccofs.sh"],
      "environment":[{"name":"RUN_DATE","value":"'"$1"'"}]}}]}' \
    --timeout attemptDurationSeconds=57600 \
    ${2:+--depends-on jobId=$2} --query jobId --output text
}
J1=$(submit 20260701)
J2=$(submit 20260702 "$J1")
```

**4. Resume.** After the day's 16:00 America/New_York has passed and the chain is
submitted through the latest missing date, run `set_schedule ENABLED`.
Resuming earlier submits a duplicate job for the same date.
