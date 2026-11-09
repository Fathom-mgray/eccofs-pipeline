# ECCOFS technical reference

The model binaries, the steps of a run, tiling, I/O settings, data layout and
deployment. For submitting runs see [`docs/running.md`](docs/running.md); for
the platform see [`INFRASTRUCTURE.md`](INFRASTRUCTURE.md); for the image build
see [`container/TECHNICAL.md`](container/TECHNICAL.md).

## The three binaries

Built from the ROMS source in the base image by
[`container/Dockerfile`](container/Dockerfile). They differ in CPP header and
`ROMS_APPLICATION`.

| Binary | Header | Grid (Lm × Mm × N) | Role |
|---|---|---|---|
| `romsM_nl_v2` | `eccofs_nl_v2.h` | 1665 × 1441 × 50 (3 km) | forward model: outer loop 0 and the re-run after each increment |
| `romsM_da_v2` | `eccofs_da_v2.h` | 832 × 720 × 50 (6 km) | `SPLIT_RBL4DVAR` increment, at the coarser resolution |
| `romsM_fwd_pio_v2_sta` | `eccofs_fwd_v2.h` | 1665 × 1441 × 50 (3 km) | forecast |

All three share one CPP set (`SPLIT_RBL4DVAR`, `WEAK_CONSTRAINT`, `RPCG`,
`GLS_MIXING`, `SSH_TIDES`/`UV_TIDES`, `BULK_FLUXES`, `PIO_LIB`, ...; the model
prints the full list at startup) and `Nouter=2`, `Ninner=8`. The forward binary
uses `SINGLE_PRECISION`; the DA binary uses `DOUBLE_PRECISION`.

## Steps of a run

`scripts/run_eccofs.sh` runs on the main node:

| Step | What |
|---|---|
| 0 | copy config, grids and ERA5 from S3 to EFS |
| 1 | resolve dates, create the shared workdir |
| 2 | stage the day's obs and forcing from `s3://fathom-eccofs` (4 h time limit) |
| 3 | fetch the day's two std-background files and the prior state, build the `.in` files from the templates |
| 4 | outer loop 0: 3 km forward run |
| 5 | outer loops 1-2: 6 km DA increment, `coarse2fine_ini0.py`, 3 km re-run |
| 6 | write the next three days' priors to EFS |
| 7 | forecast |
| 8 | extract the qck and sta products |
| 9 | add MLD and OHC to the avg files |
| 10 | publish (see `PUBLISH` in [`docs/running.md`](docs/running.md)) |
| 11 | list the restart files, delete the workdir, prune old run directories |

If an outer loop ends with fewer than 37 records, step 6 uses the last complete
loop (or the forward run alone) for the priors.

## Tiling

The templates hardcode `NtileI == 16`, `NtileJ == 12` (192 ranks). ROMS needs
`NtileI*NtileJ == NP`, so STEP 3 keeps `NtileJ = 12` and sets `NtileI = NP/12`.
The daily run uses 384 ranks (32 × 12). Any `NP` that is a multiple of 12 works.

## NetCDF format

PIO's `PIO_iotype_netcdf` cannot open enhanced-model NetCDF-4 files, which is
what the upstream pipeline writes. STEP 2 converts every staged file with
`ncks --fl_fmt=netcdf4_classic`. The splitrivers file first loses its `rivers`
global attribute and its `river_temp` variable; both are classic-incompatible
and unused (temperature point sources are off). Static grids, nudging
coefficients, tides and std-background files are already classic.

## Staging

`copy_assimilation_mercator_from_s3.py` waits for upstream files, up to 4 tries
30 minutes apart. Upstream can overwrite a published file in place (for example
replacing a HRRR-only wind file with a longer GFS-appended one). After all files
are present, the script compares each file's S3 ETag with the one recorded at
download, re-fetches changed files (2 rounds), and fails if they keep changing.
This matters because a stale forcing file is valid but short, and ROMS only
fails once the model clock passes its end.

## Success check

The run script treats `ROMS: DONE` in a model's log as success, even if the exit
code is non-zero: an HDF5 error can follow a complete finalize. A model that
leaves an empty log is relaunched (`MAX_RETRIES`).

## I/O settings

| Template | INP_LIB | OUT_LIB | PIO_IOTASKS | PIO_STRIDE | PIO_BASE |
|---|---|---|---|---|---|
| `roms_nl_eccofs.in` | 2 | 2 | 1 | 1 | 0 |
| `roms_da_eccofs.in` | 2 | 2 | 4 | 48 | 0 |
| `roms_nl_eccofs_forecast.in` | 1 | 2 | 3 | 32 | 1 |

`INP_LIB`/`OUT_LIB`: 1 = classic NetCDF, 2 = PIO. All use `PIO_METHOD = 2`.

## Storage

EFS `/models/eccofs`:

| Path | Contents |
|---|---|
| `data/config/` | `config/` and `python/` from this repo |
| `data/data/` | grids, nudging coefficients, tides, background-error covariance |
| `data/forcing/era5/` | `frc_eccofs_ERA5_allflux_<year>.nc4`, 2011-2023 |
| `runs/<date>/restart/` | `eccofs_ini_prior_<next-date>.nc`, written by STEP 6 and read by the next day |
| `runs/<date>/work/` | shared workdir, recreated each run |

The workdir is on EFS because every rank opens the `.in` files and checks every
input file; with node-local workdirs, nodes diverge and stop at the first
inter-node broadcast. The templates' `/scratch/<date>` paths are a per-node
symlink to the EFS workdir.

Only `data/data/grid/grid_eccofs_3km_08_b7_w2.nc` (3 km) and
`data/data/grid/grid_eccofs_6km_08_b7.nc` (6 km) are read; the other grid files
under `data/data/` are older versions.

S3:

| Location | Contents |
|---|---|
| `s3://fathom-hpc-data/models/eccofs/` | `jobs/` (run script), `data/` (source of the EFS tree), `data.manifest.yaml` |
| `s3://fathom-eccofs/` | daily input (`mercator_eccofs/`, `assimilation_files/`); products (`qck_files/`, `sta_nowcast/`, `outputs/`); `archive/ice` (icechunk) |
| `noaa-nos-eccofs-pds` | NOAA NODD: `avg/`, `qck/`, `his/`, `sta/`, `frc/` (the 9 merged forcing files), `obs/` (assimilated obs), `riv/` (the splitrivers file) |

## Deploying

Nothing in `scripts/`, `python/` or `config/` is in the image. CI runs
[`deploy.sh`](deploy.sh) on every merge to `main` that touches them, and a run
reads them from S3 at start.

| Repo | S3 (`fathom-hpc-data`) |
|---|---|
| `scripts/*.sh` | `models/eccofs/jobs/` |
| `python/*.py`, `config/*.in`, `config/*.yaml` | `models/eccofs/data/config/` |
| `data.manifest.yaml` | `models/eccofs/data.manifest.yaml` |

`deploy.sh` never deletes from S3. `./deploy.sh --dryrun` lists every file as
changed, because local timestamps are newer. The image
(`container/`) is rebuilt by the `eccofs-docker-image` CI job.
