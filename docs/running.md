# Running the pipeline

Every run is an AWS Batch job whose command is the S3 key of
[`scripts/run_eccofs.sh`](../scripts/run_eccofs.sh). The job definition has no
default command, so always pass the key. Nodes, queues and the `hpc` command
come from the platform ([`INFRASTRUCTURE.md`](../INFRASTRUCTURE.md)).

## Submitting

Test run (shortened, about 75 min):

```bash
hpc batch -c eccofs -m eccofs -N 4 -e TEST_MODE=1 -e RUN_DATE=20260623 \
    models/eccofs/jobs/run_eccofs.sh
```

Full run (about 9.5 h, so it needs a timeout above the 2 h default):

```bash
hpc batch -c eccofs -m eccofs -N 4 --timeout 57600 models/eccofs/jobs/run_eccofs.sh
```

`-N` sets the node count. Any count works if the rank count is a multiple of
12 ([`TECHNICAL.md`](../TECHNICAL.md#tiling)).

## Settings

Pass with `-e NAME=value`.

| Variable | Meaning |
|---|---|
| `RUN_DATE` | `YYYYMMDD`; default is today minus 3 days, US/Eastern |
| `PUBLISH` | `production` (default): NODD, icechunk archive, Slack. `validation`: only `s3://fathom-eccofs/<VALIDATION_PREFIX>/<date>/`, to compare with production. `none`: nothing leaves the AWS account |
| `VALIDATION_PREFIX` | default `pio-validation` |
| `TEST_MODE` | shorten to `TEST_NTIMES` steps (default 20) and publish nothing |
| `KEEP_WORKDIR` | keep `runs/<date>/work` after the run |
| `KEEP_RUNS` | run directories to keep on EFS, default 3 |
| `MAX_RETRIES` | relaunches of a model that left an empty log, default 2 |
| `SLACK_WEBHOOK_URL` | post a summary to Slack when the run ends; unset means no post |
| `NODD_SECRET_ID` | Secrets Manager id of the NODD credentials, default `eccofs-hpc/nodd-credentials` |

Every mode, including `TEST_MODE`, writes next-day restart files to
`/models/eccofs/runs/<date>/restart/`, the chain later runs start from. Running
a date production has already finished overwrites that date's restart files.

## The daily schedule

At 16:00 America/New_York the schedule submits a 4-node, 16-hour job with no
`RUN_DATE`, so it processes today minus 3 days. If a day fails or is skipped,
the next day fails too, since each run starts from the previous day's restart
file. See [`operations.md`](operations.md#recovering-the-daily-chain).
