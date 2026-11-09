# eccofs image

The production image for ECCOFS: the three ROMS binaries (3 km forward, 6 km
data assimilation, 3 km forecast) and the Python/NCO environment, on top of the
platform's `roms-base` image. The run script, templates and helpers are not in
the image; they come from S3 ([`../TECHNICAL.md`](../TECHNICAL.md#deploying)).

See [`TECHNICAL.md`](TECHNICAL.md) for the build.
