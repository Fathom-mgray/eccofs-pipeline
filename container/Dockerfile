# hpc/roms-base + the three ECCOFS ROMS binaries + Python/NCO -- see TECHNICAL.md.

ARG BASE_IMAGE
FROM ${BASE_IMAGE}

# Python + NCO via micromamba -- see TECHNICAL.md ("micromamba environment").
RUN curl -fsSL --retry 5 https://micro.mamba.pm/api/micromamba/linux-64/latest \
      | tar -xj -C /usr/local bin/micromamba \
&&  micromamba create -y -p /opt/conda-eccofs -c conda-forge \
      python=3.11 numpy scipy netcdf4 boto3 pytz nco xarray gsw icechunk h5netcdf \
&&  micromamba clean -a -y \
&&  /opt/conda-eccofs/bin/python -c "import netCDF4, numpy, scipy, boto3, pytz, xarray, gsw, icechunk, h5netcdf" \
&&  /opt/conda-eccofs/bin/ncrcat --version 2>&1 | head -1

ENV PATH=$PATH:/opt/eccofs/bin:/opt/conda-eccofs/bin

# -ffast-math stripped -- see TECHNICAL.md ("-ffast-math is stripped").
RUN sed -i 's|^\([[:space:]]*\)FFLAGS += -ffast-math|\1#FFLAGS += -ffast-math|' \
      /opt/roms/Compilers/Linux-gfortran.mk \
&&  ! grep -q '^[[:space:]]*FFLAGS += -ffast-math' /opt/roms/Compilers/Linux-gfortran.mk

# Three separate compiles, one layer each -- see TECHNICAL.md ("Three compiles").
ARG ROMS_EXTRA_CPPDEFS=""
COPY build_one.sh /tmp/build_one.sh
RUN mkdir -p /opt/eccofs/bin

RUN /tmp/build_one.sh ECCOFS_NL_V2  eccofs_nl_v2.h  romsM_nl_v2
RUN /tmp/build_one.sh ECCOFS_DA_V2  eccofs_da_v2.h  romsM_da_v2
RUN /tmp/build_one.sh ECCOFS_FWD_V2 eccofs_fwd_v2.h romsM_fwd_pio_v2_sta

RUN ls -l /opt/eccofs/bin/
