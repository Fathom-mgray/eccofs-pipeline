# eccofs image: technical reference

| Path | What |
|---|---|
| `/opt/eccofs/bin/romsM_nl_v2` | 3 km forward model |
| `/opt/eccofs/bin/romsM_da_v2` | 6 km `SPLIT_RBL4DVAR` increment |
| `/opt/eccofs/bin/romsM_fwd_pio_v2_sta` | 3 km forecast |
| `/opt/conda-eccofs` | Python 3.11 (numpy, scipy, xarray, gsw, netCDF4, boto3, icechunk, h5netcdf) and NCO |

The job contract (the Batch command is the S3 key of the run script) comes from
the base image; see [`../INFRASTRUCTURE.md`](../INFRASTRUCTURE.md#container-image).

## micromamba environment

Amazon Linux 2023 has no NCO package, so Python and NCO come from a micromamba
environment. It is appended to `PATH`, and the run script calls
`/opt/conda-eccofs/bin/python` directly.

## `-ffast-math` is stripped

`roms-base` compiles with `-ffast-math`, which sends LAPACK's `dlamch` probe
(inside `rpcg_lanczos`, part of the DA increment) into an infinite loop on
rank 0. The Dockerfile removes the flag before building and checks that it is
gone. If a DA run hangs on rank 0 in the Lanczos step, check that an upstream
`roms-base` change hasn't restored it.

## Three compiles

[`build_one.sh`](build_one.sh) builds one binary from its CPP header. Each
binary gets its own Dockerfile layer, so a failure names the application and
the finished builds stay cached. A build takes 15-40 minutes in total.

Build argument `ROMS_EXTRA_CPPDEFS` (for example `-DFOO -DBAR`) is added to all
three builds. Two kinds of change need different rebuilds:

- **CPP toggles**: set `ROMS_EXTRA_CPPDEFS`; one build of this image.
- **ROMS source or compiler settings** (headers, `Compilers/*.mk`): change
  `roms-base` on the platform first; CI then rebuilds this image from it
  ([`../INFRASTRUCTURE.md`](../INFRASTRUCTURE.md#ci)).

## First deployment

The job definition mounts EFS at `/models/eccofs`, which must already exist.
Run `hpc data sync eccofs` once before the first job.
