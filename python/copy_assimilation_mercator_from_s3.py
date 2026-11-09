#!/usr/bin/env python3
"""
Download one day's ECCOFS observation and forcing files from S3.

Reads RUN_DATE (YYYYMMDD) from the environment; the default is today minus
3 days, US/Eastern. Files go to /scratch/<RUN_DATE>/input/OBS/.

Files that upstream has not published yet are retried (4 attempts, 30 minutes
apart). Once everything is present, each file is checked once more for an
in-place overwrite upstream and re-fetched if it changed -- see TECHNICAL.md
("Staging").
"""

import boto3
import os
import pytz
import time
import sys
from datetime import datetime, timedelta
from botocore.exceptions import ClientError

# ── Date ──────────────────────────────────────────────────────────────────────
if os.environ.get("RUN_DATE"):
    target_date = os.environ["RUN_DATE"]
    print(f"Target Date (from env): {target_date}")
else:
    eastern = pytz.timezone("US/Eastern")
    target_date = (datetime.now(eastern) - timedelta(days=3)).strftime("%Y%m%d")
    print(f"Target Date (auto): {target_date}")

# ── Configuration ─────────────────────────────────────────────────────────────
source_buckets = ["fathom-eccofs-us-east-2", "fathom-eccofs"]
source_prefix = "assimilation_files/"
local_dest = f"/scratch/{target_date}/input/OBS/"
max_retries = 4
wait_time_seconds = 30 * 60  # 30 minutes
max_revalidation_rounds = 2

os.makedirs(local_dest, exist_ok=True)
print(f"Writing to: {local_dest}")

# ── Expected files ────────────────────────────────────────────────────────────
expected_files = [
    f"ECCOFS_REALTIME_{target_date}_bry.nc",
    f"ECCOFS_REALTIME_{target_date}_clm.nc",
    f"ECCOFS_REALTIME_CONTINUE_lwrad_{target_date}.nc",
    f"ECCOFS_REALTIME_CONTINUE_lwrad_down_{target_date}.nc",
    f"ECCOFS_REALTIME_CONTINUE_Pair_{target_date}.nc",
    f"ECCOFS_REALTIME_CONTINUE_Qair_{target_date}.nc",
    f"ECCOFS_REALTIME_CONTINUE_rain_{target_date}.nc",
    f"ECCOFS_REALTIME_CONTINUE_swrad_{target_date}.nc",
    f"ECCOFS_REALTIME_CONTINUE_Tair_{target_date}.nc",
    f"ECCOFS_REALTIME_CONTINUE_Uwind_{target_date}.nc",
    f"ECCOFS_REALTIME_CONTINUE_Vwind_{target_date}.nc",
    f"ECCOFS_REALTIME_MERGED_lwrad_{target_date}.nc",
    f"ECCOFS_REALTIME_MERGED_lwrad_down_{target_date}.nc",
    f"ECCOFS_REALTIME_MERGED_Pair_{target_date}.nc",
    f"ECCOFS_REALTIME_MERGED_Qair_{target_date}.nc",
    f"ECCOFS_REALTIME_MERGED_rain_{target_date}.nc",
    f"ECCOFS_REALTIME_MERGED_swrad_{target_date}.nc",
    f"ECCOFS_REALTIME_MERGED_Tair_{target_date}.nc",
    f"ECCOFS_REALTIME_MERGED_Uwind_{target_date}.nc",
    f"ECCOFS_REALTIME_MERGED_Vwind_{target_date}.nc",
    f"ECCOFS_splitrivers_file_3km_{target_date}.nc",
    f"ssh_sst_cmems_v2_{target_date}_3km.nc",
    f"ssh_sst_cmems_v2_{target_date}.nc",
]

# Mercator boundary-condition file
mercator_date = datetime.strptime(target_date, "%Y%m%d").strftime("%Y-%m-%d")
mercator_file = f"mercator_{mercator_date}.nc"
mercator_buckets = ["fathom-eccofs-us-east-2", "fathom-eccofs"]
mercator_prefix = "mercator_eccofs/"

# ── Helpers ───────────────────────────────────────────────────────────────────
# filename -> (bucket, key, ETag) for each staged file, used for the final recheck.
downloaded_meta = {}


def get_missing_files():
    missing = []
    for filename in expected_files:
        local_path = os.path.join(local_dest, filename)
        if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
            missing.append(filename)
    return missing


def download_with_fallback(s3_client, buckets, key, local_path, label):
    """Try each bucket in order. Returns (downloaded, had_non_404_error, bucket, etag)."""
    had_non_404_error = False

    for bucket in buckets:
        try:
            head = s3_client.head_object(Bucket=bucket, Key=key)
        except ClientError as e:
            if e.response["Error"].get("Code") in ("404", "NoSuchKey"):
                print(f"  [PENDING] {label} not found in {bucket}.")
                continue
            had_non_404_error = True
            print(f"  [WARNING] HEAD failed for {label} from {bucket}: {e}")
            continue

        try:
            s3_client.download_file(bucket, key, local_path)
            print(f"  [DOWNLOADED] {label} (from {bucket}, ETag {head['ETag']})")
            return True, False, bucket, head["ETag"]
        except Exception as e:
            had_non_404_error = True
            print(f"  [WARNING] Failed downloading {label} from {bucket}: {e}")

    return False, had_non_404_error, None, None


def seed_preexisting_meta(s3_client, files):
    """Record the S3 ETag of files already on disk (left by an earlier attempt of
    this job) so revalidate_downloads() can check them too."""
    for filename, key, buckets in files:
        local_path = os.path.join(local_dest, filename)
        if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
            continue
        for bucket in buckets:
            try:
                head = s3_client.head_object(Bucket=bucket, Key=key)
            except ClientError:
                continue
            downloaded_meta[filename] = (bucket, key, head["ETag"])
            break


def revalidate_downloads(s3_client, max_rounds=max_revalidation_rounds):
    """Compare each staged file's ETag with S3 and re-fetch any that changed.
    Returns False if files are still changing after max_rounds."""
    stale = []
    for round_num in range(1, max_rounds + 1):
        stale = []
        for filename, (bucket, key, etag) in list(downloaded_meta.items()):
            try:
                current = s3_client.head_object(Bucket=bucket, Key=key)
            except ClientError as e:
                print(f"  [WARNING] revalidate: HEAD failed for {filename}: {e}")
                continue
            if current["ETag"] != etag:
                print(
                    f"  [STALE] {filename} changed on S3 since download "
                    f"(ETag {etag} -> {current['ETag']}) -- re-fetching"
                )
                stale.append(filename)

        if not stale:
            print(f"Revalidation pass {round_num}: all {len(downloaded_meta)} files still current.")
            return True

        for filename in stale:
            bucket, key, _ = downloaded_meta[filename]
            local_path = os.path.join(local_dest, filename)
            downloaded, _, new_bucket, new_etag = download_with_fallback(
                s3_client, [bucket], key, local_path, filename
            )
            if downloaded:
                downloaded_meta[filename] = (new_bucket, key, new_etag)

    print(f"FATAL ERROR: {stale} still changing on S3 after {max_rounds} revalidation rounds.")
    return False


s3 = boto3.client("s3")

# ── Download Mercator file ────────────────────────────────────────────────────
mercator_local = os.path.join(local_dest, mercator_file)
mercator_key = f"{mercator_prefix}{mercator_file}"
if not os.path.exists(mercator_local) or os.path.getsize(mercator_local) == 0:
    print(f"Downloading Mercator file: {mercator_file}")
    downloaded, had_non_404_error, bucket, etag = download_with_fallback(
        s3, mercator_buckets, mercator_key, mercator_local, mercator_file,
    )
    if downloaded:
        downloaded_meta[mercator_file] = (bucket, mercator_key, etag)
    else:
        if had_non_404_error:
            print(f"  [ERROR] Failed to fetch Mercator file from any bucket: {mercator_file}")
            sys.exit(1)
        print(f"  [WARNING] Mercator file not found in either bucket: {mercator_file}")
else:
    print(f"  [CACHED] {mercator_file}")

# Baseline ETags for files already on disk.
seed_preexisting_meta(
    s3,
    [(mercator_file, mercator_key, mercator_buckets)]
    + [(f, f"{source_prefix}{f}", source_buckets) for f in expected_files],
)

# ── Retry loop for the OBS files ─────────────────────────────────────────────
for attempt in range(1, max_retries + 1):
    print(f"\n--- Download Attempt {attempt} of {max_retries} ---")

    missing_files = get_missing_files()

    if not missing_files:
        print("All expected files are present and successfully downloaded.")
        if not revalidate_downloads(s3):
            sys.exit(1)
        sys.exit(0)

    print(f"Missing {len(missing_files)} files. Connecting to S3...")

    for filename in missing_files:
        s3_key = f"{source_prefix}{filename}"
        local_path = os.path.join(local_dest, filename)
        downloaded, had_non_404_error, bucket, etag = download_with_fallback(
            s3, source_buckets, s3_key, local_path, filename
        )
        if downloaded:
            downloaded_meta[filename] = (bucket, s3_key, etag)
        elif had_non_404_error:
            print(f"  [ERROR] Failed to fetch {filename} from any bucket.")

    remaining_missing = get_missing_files()
    if not remaining_missing:
        print("\nSuccess! All files downloaded.")
        if not revalidate_downloads(s3):
            sys.exit(1)
        sys.exit(0)

    if attempt < max_retries:
        print(f"\nStill missing {len(remaining_missing)} files.")
        print("Waiting 30 minutes before trying again...")
        time.sleep(wait_time_seconds)

print(f"\nFATAL ERROR: Failed to download all files after {max_retries} attempts.")
print("The following files were never retrieved:", remaining_missing)
sys.exit(1)
