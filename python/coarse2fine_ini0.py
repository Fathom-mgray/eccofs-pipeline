#!/usr/bin/env python3
"""
Add the 6 km data-assimilation increment to the 3 km initial state.

Usage: coarse2fine_ini0.py --day YYYYMMDD [--output logfile]

Reads eccofs6km_ini_<day>.nc (the difference between its time records 1 and 2,
counting from 0, is the increment for zeta, temp, salt, u, v), interpolates it onto the 3 km grid and
adds it in place to eccofs_ini_<day>.nc. Land points are filled with the
nearest water value before interpolating; ubar and vbar get no increment.
"""

import sys
import optparse
import time

import numpy as np
from netCDF4 import Dataset
from scipy.interpolate import NearestNDInterpolator, RegularGridInterpolator

from roms_get_grid import roms_get_grid

def coarse2fine_ini0():
    # parse command line arguments
    parser = optparse.OptionParser()
    parser.add_option("-s", "--day", dest="day", help="Run date, YYYYMMDD")
    parser.add_option("-o", "--output", dest="output", help="Log file (default: stdout)")
    options, _ = parser.parse_args()
    if options.output:
        sys.stdout = open(options.output, 'w')
    if options.day:
        day = options.day
    else:
        sys.exit("--day is required")

    file6km = f"eccofs6km_ini_{day}.nc"    # input coarse grid
    file3km = f"eccofs_ini_{day}.nc"       # output fine grid
    
    print(f" Processing file {file6km}")

    # Grids for the coarse (input) and fine (output) data
    grd_eccofs3km = '/models/eccofs/data/data/grid/grid_eccofs_3km_08_b7_w2.nc'
    grd_eccofs6km = '/models/eccofs/data/data/grid/grid_eccofs_6km_08_b7.nc'
    scoord = [7, 2, 250, 50, 2, 4]

    C = roms_get_grid(grd_eccofs6km,scoord)
    C['name'] = 'eccofs6km'
    F = roms_get_grid(grd_eccofs3km,scoord)
    F['name'] = 'eccofs'
    
    # The fine grid refines the whole coarse grid by a factor of 2
    L, M = C['lon_rho'].shape
    F['parent_Imin'] = 0; F['parent_Imax'] = L-1; F['parent_Jmin'] = 0; F['parent_Jmax'] = M-1
    F['refine_factor'] = 2
    
    
    # Obtain increments from the coarse grid file
    tindex = 1
    INC = {}
    INC['eccofs6km'] = {}
    INC['eccofs'] = {}
    
    with Dataset(file6km, 'r') as nc:
        # Process two-dimensional variables
        for vname in ['zeta']:
            data = nc.variables[vname][tindex:tindex+2,:, :]  # zero-based index to get 2 time levels
            INC['eccofs6km'][vname] = np.squeeze(data[1, :, :] - data[0, :, :])
            
        # Process three-dimensional variables
        for vname in ['temp', 'salt', 'u', 'v']:
            data = nc.variables[vname][tindex:tindex+2,:, :, :]
            INC['eccofs6km'][vname] = np.squeeze(data[1, :, :, :]- data[0, :, :, :])
   
    INC = increments_coarse2fine(C, F, INC)
    
    INC['eccofs']['ubar'] = np.zeros(F['mask_u'].shape)
    INC['eccofs']['vbar'] = np.zeros(F['mask_v'].shape)
    
    nc = Dataset(file3km, 'a')
    
    # Add the increment
    for vname in ['zeta', 'ubar', 'vbar']:
        data = nc.variables[vname]
        data[0,:,:] = data[0,:,:] + INC['eccofs'][vname] 
        print(f" wrote {vname} : range = {data[0,:].min()} to {data[0,:].max()} ")
         
    for vname in ['temp','salt','u','v']:
        data = nc.variables[vname]
        data[0,:,:,:] = data[0,:,:,:] + INC['eccofs'][vname] 
        print(f" wrote {vname} : range = {data[0,:].min()} to {data[0,:].max()} ")

    nc.close()


def increments_coarse2fine(C, F, INC):
    """Interpolate the 6 km increments in INC to the 3 km grid (nearest-water fill, then linear)."""
    L, M = C['h'].shape
    N = C['N']

    # coarse grid indices for rho-, u-, and v- points
    IrC = np.arange(M); JrC = np.arange(L)
    IpC = np.arange(0.5, M-1, 1); JpC = np.arange(0.5, L-1, 1)
    YrC, XrC = np.meshgrid(IrC, JrC)             # RHO points
    YuC, XuC = np.meshgrid(IpC, JrC)   # U points
    YvC, XvC = np.meshgrid(IrC, JpC)   # V points 
    

    
    # refinement factors
    Gfactor = F['refine_factor']
    Imin = F['parent_Imin']
    Imax = F['parent_Imax']
    Jmin = F['parent_Jmin']
    Jmax = F['parent_Jmax']
    
    delta = 1.0 / Gfactor
    halfdelta = 0.5 * delta

    # fine grid indices for rho-, u-, and v- points
    IrF = np.arange(Imin, Imax + delta, delta)  # RHO-points
    JrF = np.arange(Jmin, Jmax + delta, delta)  # RHO-points

    IpF = np.arange(Imin + halfdelta, Imax - halfdelta + delta, delta)  # PSI-points
    JpF = np.arange(Jmin + halfdelta, Jmax - halfdelta + delta, delta)  # PSI-points   
    IpF[0]=delta; JpF[0]=delta; IpF[-1]=Imax-delta; JpF[-1]=Jmax-delta

    YrF, XrF = np.meshgrid(JrF, IrF)  # RHO-points
    YuF, XuF = np.meshgrid(JpF, IrF)
    YvF, XvF = np.meshgrid(JrF, IpF)  # V-points
    
    # --------rho points  zeta, temp and salt--------
    # Flattened array of consequtive indexes for rho point
    arr = np.arange(len(C['mask_rho'].ravel()))
    nonan = np.where(C['mask_rho'].ravel() == 1)[0]  # identify water points on Coarse grid
    all_pts = np.column_stack((XrC.ravel()[nonan], YrC.ravel()[nonan]))
    nearest_interpolator = NearestNDInterpolator(all_pts, arr[nonan])
    arr1 = nearest_interpolator(XrC.ravel(),YrC.ravel()).astype(int)
    ind_r = np.where(C['mask_rho'].ravel() == 0)[0]
    arr_r = arr1[ind_r]
    
   
    #------- u points-------------------
    arr = np.arange(len(C['mask_u'].ravel()))
    nonan = np.where(C['mask_u'].ravel() == 1)[0]  # identify water points on Coarse grid
    all_pts = np.column_stack((XuC.ravel()[nonan], YuC.ravel()[nonan]))
    nearest_interpolator = NearestNDInterpolator(all_pts, arr[nonan])
    arr1 = nearest_interpolator(XuC.ravel(),YuC.ravel()).astype(int)
    ind_u = np.where(C['mask_u'].ravel() == 0)[0]
    arr_u = arr1[ind_u]

 
 
    #------- v points-------------------
    arr = np.arange(len(C['mask_v'].ravel()))
    nonan = np.where(C['mask_v'].ravel() == 1)[0]  # identify water points on Coarse grid
    all_pts = np.column_stack((XvC.ravel()[nonan], YvC.ravel()[nonan]))
    nearest_interpolator = NearestNDInterpolator(all_pts, arr[nonan])
    arr1 = nearest_interpolator(XvC.ravel(),YvC.ravel()).astype(int)
    ind_v = np.where(C['mask_v'].ravel() == 0)[0]
    arr_v = arr1[ind_v]

    # ----------------now interpolate -----
    # interpolate zeta values on rho grid
    all_pts = np.stack([XrF.ravel(), YrF.ravel()], axis=-1)
    vname = 'zeta'
    data = INC['eccofs6km'][vname].ravel()
    data[ind_r]=data[arr_r];     # extrapolate on Coarse grid
    data2d = data.reshape(C['mask_rho'].shape)
    interp = RegularGridInterpolator((JrC, IrC), data2d, fill_value=0)
    INC['eccofs'][vname]= interp(all_pts).reshape(F['mask_rho'].shape) * F['mask_rho']
   
    
    # interpolate temp and salt on rho grid
    for vname in ['temp', 'salt']:
        data = INC['eccofs6km'][vname]  # 3D array
        INC['eccofs'][vname] = np.zeros((N, *XrF.shape))
        for i in range(N):
            vk = np.squeeze(data[i, :, :]).ravel()
            vk[ind_r]=vk[arr_r]
            vk2d = vk.reshape(C['mask_rho'].shape)
            interp = RegularGridInterpolator((JrC, IrC), vk2d, fill_value=0)
            INC['eccofs'][vname][i,:,:]=interp(all_pts).reshape(F['mask_rho'].shape) * F['mask_rho']
    
    # interpolate u on u-grid
    all_pts = np.stack([XuF.ravel(), YuF.ravel()], axis=-1)
    vname = 'u'
    data = INC['eccofs6km'][vname] 
    INC['eccofs'][vname] = np.zeros((N, *XuF.shape))

    for i in range(N):  
        vk = np.squeeze(data[i, :, :]).ravel()
        vk[ind_u]=vk[arr_u]
        vk2d = vk.reshape(C['mask_u'].shape)
        interp = RegularGridInterpolator((JrC, IpC), vk2d, method='linear', fill_value=0)
        INC['eccofs'][vname][i,:,:]=interp(all_pts).reshape(F['mask_u'].shape) * F['mask_u']
 
    # interpolate v on v-grid
    all_pts = np.stack([XvF.ravel(), YvF.ravel()], axis=-1)
    vname = 'v'
    data = INC['eccofs6km'][vname] 
    INC['eccofs'][vname] = np.zeros((N, *XvF.shape))

    for i in range(N):  
        vk = np.squeeze(data[i, :, :]).ravel()
        vk[ind_v]=vk[arr_v]
        vk2d = vk.reshape(C['mask_v'].shape)
        interp = RegularGridInterpolator((JpC, IrC), vk2d, method='linear', fill_value=0)
        INC['eccofs'][vname][i,:,:]=interp(all_pts).reshape(F['mask_v'].shape) * F['mask_v']
         
    return INC

if __name__ == "__main__":
    start = time.time()
    coarse2fine_ini0()
    end = time.time()
    print(f"Elapsed time: {end - start:.4f} seconds")

