# Infrastructure contract

What this pipeline uses from the HPC platform
([`fathomscience/aws-batch-hpc`](https://gitlab.com/fathomscience/aws-batch-hpc)).
The repo reaches the platform only through the names below. If one changes on
the platform side, this file and the code that uses it change together.

## S3

Bucket `fathom-hpc-data` (`S3_BUCKET` in every job), region `us-east-2`:

| Key | Written by | Read by |
|---|---|---|
| `models/eccofs/jobs/*.sh` | `deploy.sh` (from `scripts/`) | the Batch job: its command is one of these keys |
| `models/eccofs/data/config/` | `deploy.sh` (from `python/`, `config/`) | STEP 0 sync to EFS |
| `models/eccofs/data/data/`, `forcing/era5/` | uploaded by hand | STEP 0 sync to EFS |
| `models/eccofs/data/forcing/std_background/` | uploaded by hand | STEP 3 fetches the day's two files |
| `models/eccofs/data.manifest.yaml` | `deploy.sh` | the platform's `hpc data sync eccofs` |

Other buckets the job role can reach:
- `fathom-eccofs`, `fathom-eccofs-us-east-2`: daily obs and forcing input, publish prefixes, icechunk archive.
- `noaa-nos-eccofs-pds`: NOAA NODD, written with the credentials in Secrets Manager secret `eccofs-hpc/nodd-credentials`.

## Container image

CI builds `hpc/eccofs` from [`container/`](container/) on the platform's `hpc/roms-base:latest`; jobs run tag `latest`. The base image provides:
- `/opt/hpc/run-script.sh`: downloads the job's S3 key and runs it on the main node, after setting `HOSTFILE`, `NP`, `RANKS_PER_NODE`, `AWS_BATCH_JOB_NUM_NODES` and `S3_BUCKET` and putting Open MPI with EFA on `PATH`.
- `/opt/rank-wrap.sh`, which raises each rank's stack and memlock limits.
- `/opt/roms`: the ROMS source with the `eccofs_{nl,da,fwd}_v2.h` headers and `build_roms.sh`, configured for gfortran, Open MPI and PIO.

## Jobs, queues, schedule

| Name | What |
|---|---|
| job definition `eccofs` | hpc6a nodes, 96 ranks each, EFS `/models/eccofs` mounted, no default command |
| job definition `eccofs-hpc8a` | hpc8a nodes, 192 ranks each, same image and mounts |
| queues `eccofs-hpc-queue`, `eccofs-hpc8a-queue` | MPI queues for those job definitions |
| schedule `eccofs-hpc-daily-eval` (group `eccofs-hpc-schedules`) | 16:00 America/New_York: `run_eccofs.sh`, 4 × `hpc6a.48xlarge`, 16 h timeout, no `RUN_DATE` |
| log group `/eccofs-hpc/jobs` | job output |

The default job timeout is 2 h; full runs pass a longer one. To get Slack
summaries from the daily run, the platform sets `SLACK_WEBHOOK_URL` in the
schedule's job environment.

## Shared filesystem

EFS is mounted at `/models/eccofs` on every node: `data/` is mirrored from S3,
`runs/<date>/restart/` holds the daily chain, `runs/<date>/work/` is the shared
workdir (linked from each node's `/scratch/<date>`).

## CI

[`.gitlab-ci.yml`](.gitlab-ci.yml):

| Job | Runs when | Does |
|---|---|---|
| `ci-image` | `ci/Dockerfile` changes on `main`, or manually | builds the runner image (`docker`, `aws-cli`, `bash`) |
| `eccofs-deploy` | a push to `main` touching `deploy.sh`, the manifest, `scripts/`, `python/` or `config/` | runs `./deploy.sh` |
| `eccofs-docker-image` | `container/Dockerfile`, `container/*.sh` or the CI file changes, manually, or on a trigger from aws-batch-hpc | builds and pushes `hpc/eccofs` (`:<sha>`, `:latest`) |

AWS access is GitLab OIDC into the platform's `GitLab-OIDC-Role`, using the
`fathomscience` group variables `AWS_ROLE_ARN` and `AWS_ACCOUNT_ID` and this
project's `AWS_REGION` (`us-east-2`). After aws-batch-hpc rebuilds `roms-base`
on `main`, its `trigger-eccofs-pipeline` job starts a pipeline here to rebuild
`hpc/eccofs`.
