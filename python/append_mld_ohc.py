"""
Add ocean heat content (ohc) and mixed-layer depth (mld) to ROMS output files.

Usage: append_mld_ohc.py eccofs_avg_*.nc ...

Each file is rewritten in place as chunked NetCDF-4 with the two new variables.
"""

import os
import shutil
import sys
import xarray as xr

from roms_tools import (
    precompute_roms_z_static,
    compute_roms_z,
    roms_ohc,
    roms_mld
)


CHUNKS = {
    't': 1,
    'eta_2d': 481,
    'xi_2d': 556,
    's': 50,
    'eta_3d': 145,
    'xi_3d': 167
}


def find_time_var(dataset: xr.Dataset):
    for name in ('time', 'ocean_time', 'bry_time', 'frc_time', 'sea_time'):
        if name in dataset.variables:
            return name
    raise ValueError('No recognised time variable found in dataset.')


def compute_timestep(dataset: xr.Dataset, static_vars: dict, t_idx):
    z_r, z_w = compute_roms_z(dataset, t_idx, static_vars)
    ohc = roms_ohc(dataset, t_idx, z_r, z_w)
    mld = roms_mld(dataset, t_idx, z_r)
    return ohc, mld


def nc4_encoding(dataset: xr.Dataset, chunks):
    sigma_dims = {'s_rho', 's_w'}
    eta_dims = {'eta_rho', 'eta_u', 'eta_v', 'eta_psi'}
    xi_dims = {'xi_rho', 'xi_u', 'xi_v', 'xi_psi'}
    time_dims = {'ocean_time', 'time', 'bry_time', 'frc_time', 'sea_time'}

    encoding = {}
    for variable in dataset.data_vars:
        v = dataset[variable]
        dims = set(v.dims)

        has_sigma = bool(dims & sigma_dims)
        has_horiz = bool((dims & eta_dims) and (dims & xi_dims))

        if not has_horiz:
            encoding[variable] = {}
            continue

        if has_sigma:
            chunks = tuple(
                CHUNKS['t'] if d in time_dims else
                CHUNKS['s'] if d in sigma_dims else
                CHUNKS['eta_3d'] if d in eta_dims else
                CHUNKS['xi_3d'] if d in xi_dims else
                v.sizes[d]
                for d in v.dims
            )
        else:
            chunks = tuple(
                CHUNKS['t'] if d in time_dims else
                CHUNKS['eta_2d'] if d in eta_dims else
                CHUNKS['xi_2d'] if d in xi_dims else
                v.sizes[d]
                for d in v.dims
            )
        
        encoding[variable] = {'chunksizes': chunks}

    return encoding

if __name__ == '__main__':
    input_files = sys.argv[1:]
    print(input_files)

    for input_file in input_files:
        ds = xr.open_dataset(input_file, decode_times=True)
        time_var = find_time_var(ds)
        ntimes = ds.sizes[ds[time_var].dims[0]]

        static = precompute_roms_z_static(ds)

        new_arrays = [compute_timestep(ds, static, t) for t in range(ntimes)]
        ohc_list = [x[0] for x in new_arrays]
        mld_list = [x[1] for x in new_arrays]

        ds['ohc'] = xr.concat(ohc_list, dim=time_var)
        ds['mld'] = xr.concat(mld_list, dim=time_var)

        var_encoding = nc4_encoding(ds, CHUNKS)
        
        tmp_output = f'{input_file}.tmp'
        ds.to_netcdf(tmp_output, format='NETCDF4', engine='netcdf4', encoding=var_encoding)
        ds.close()

        os.remove(input_file)
        shutil.move(tmp_output, input_file)
