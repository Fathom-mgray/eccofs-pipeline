# ECCOFS

ECCOFS (East Coast Coastal Ocean Forecast System) is a ROMS ocean model of the
US East Coast. It runs once a day: it assimilates satellite sea-surface height
and temperature into the model (RBL4DVAR data assimilation), then forecasts
from the corrected state. Products go to S3, the NOAA Open Data Dissemination
(NODD) bucket, and a versioned icechunk/zarr archive.

This repo holds everything that defines the science: the run script, the ROMS
input templates, the Python helpers and the model build. The compute it runs on
(AWS Batch, storage, the daily schedule) comes from
[`fathomscience/aws-batch-hpc`](https://gitlab.com/fathomscience/aws-batch-hpc)
and is listed in [`INFRASTRUCTURE.md`](INFRASTRUCTURE.md). You don't need to
know how that platform works to change the pipeline.

## One daily cycle

The schedule runs [`scripts/run_eccofs.sh`](scripts/run_eccofs.sh) for the date
three days back, when that day's observations are complete. The script:

1. stages the day's observations and forcing
2. starts from the previous day's restart file
3. runs the 3 km model forward (outer loop 0)
4. runs two outer loops of 6 km data assimilation; after each, the increment is
   interpolated to 3 km and the 3 km model is re-run
5. saves restart files for the next three days
6. runs the forecast and publishes the products

A full run takes about 9.5 hours on 384 cores.

## What's here

| Path | What |
|---|---|
| [`scripts/run_eccofs.sh`](scripts/run_eccofs.sh) | the run script (production, validation and no-publish modes) |
| [`python/`](python/) | helpers: obs staging, coarse-to-fine interpolation, MLD/OHC, archive upload |
| [`config/`](config/) | ROMS `.in` templates, 4D-Var parameters, `varinfo.yaml`, station list |
| [`container/`](container/) | build of the three ROMS binaries and the Python environment |
| [`TECHNICAL.md`](TECHNICAL.md) | pipeline reference |
| [`docs/running.md`](docs/running.md) | submitting runs, test runs, settings |
| [`docs/operations.md`](docs/operations.md) | changing config, recovering a broken daily chain |

## Making a change

- **Script, template or helper** (`scripts/`, `config/`, `python/`): edit it,
  try it with a `TEST_MODE` run ([`docs/running.md`](docs/running.md)), then
  merge to `main`. CI uploads it and the next run uses it. No rebuild.
- **Model physics or CPP options** (`container/`): rebuilds the image; see
  [`container/TECHNICAL.md`](container/TECHNICAL.md).

A `TEST_MODE` run still writes restart files into the daily chain, so test with
a date production has already finished.
