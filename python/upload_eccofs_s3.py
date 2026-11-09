"""
Append one forecast to the versioned icechunk/zarr archive.

Usage: upload_eccofs_s3.py YYYYMMDD eccofs_avg*.nc

Combines eccofs_qck_<date>_fwd.nc4 with the MLD and OHC from the avg files and
commits it to s3://fathom-eccofs/archive/ice, tagged with the date 3 days after
the run date. A date that is already tagged is skipped.
"""

import sys
import icechunk as ic
import xarray as xr

from datetime import datetime, timedelta

nc4file_date = sys.argv[1]
nc4file = f"eccofs_qck_{nc4file_date}_fwd.nc4"
nc4files_avg = sys.argv[2:]

storage = ic.s3_storage(
    bucket="fathom-eccofs",
    prefix="archive/ice",
    region="us-east-1",
)
repo = ic.Repository.open_or_create(storage)
repo_tags = list(repo.list_tags())
# Tags already include the +3 day offset (see create_tag below); do not add it again.
repo_tags = sorted([datetime.strptime(tag, "%Y%m%d") for tag in repo_tags])

tag = nc4file.split('_')[-2]
tag = datetime.strptime(tag, "%Y%m%d") + timedelta(days=3)

if tag not in repo_tags:
    try:
        ds = xr.open_dataset(nc4file)
    except Exception:
        print("File is not local; reading it from S3.")
        ds = xr.open_dataset(
            f"s3://fathom-eccofs/qck_files/{nc4file}",
            engine="h5netcdf",
            backend_kwargs={"storage_options": {"anon": False}}
        )
        print(f"{nc4file} successfully opened")
    session = repo.writable_session("main")

    ohc_chunks = []
    mld_chunks = []

    for file in nc4files_avg:
        try:
            ds_avg = xr.open_dataset(file)
        except Exception:
            ds_avg = xr.open_dataset(
                f"s3://noaa-nos-eccofs-pds/avg/{file}",
                engine="h5netcdf",
                backend_kwargs={"storage_options": {"anon": True}}
            )
        print(f"collecting ohc and mld from {file}")
        # Load now: the S3 fallback cannot read lazily after the file is closed.
        ohc_chunks.append(ds_avg.ohc.load())
        mld_chunks.append(ds_avg.mld.load())
        ds_avg.close()
    print("concatenating timesteps")
    ds["ohc"] = xr.concat(ohc_chunks, dim="ocean_time")
    ds["mld"] = xr.concat(mld_chunks, dim="ocean_time")

    print("converting to zarr store")

    ds.to_zarr(
        session.store,
        mode='w',
        consolidated=False,
    )

    snapshot_id = session.commit(f"Added forecast from {nc4file}")
    repo.create_tag(tag.strftime("%Y%m%d"), snapshot_id)
    ds.close()

    print(f"File {nc4file} processed with tag {tag}.")

else:
    print(f"File {nc4file} has already been processed.")
