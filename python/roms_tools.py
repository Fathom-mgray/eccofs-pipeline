"""
roms_tools.py
Vectorised ROMS diagnostic functions (Vtransform=2).

Public API
----------
Batch-optimised (use in processing loops):
    roms_get_time_index(ds, dstr)         -> int
    precompute_roms_z_static(ds)          -> dict
    compute_roms_z(ds, t_idx, static)     -> (z_r, z_w)
    roms_mld(ds, t_idx, z_r)             -> xr.DataArray
    roms_ohc(ds, t_idx, z_r, z_w)        -> xr.DataArray
    roms_zint(data, z_r, z_w, *args)     -> (vint, thickness, dz_out)

Convenience wrappers (single call, any context):
    roms_mld_from_ds(ds, time_req)        -> xr.DataArray
    roms_ohc_from_ds(ds, time_req)        -> xr.DataArray
"""

import numpy as np
import pandas as pd
import xarray as xr
import gsw


# =========================================================================== #
#  Time index                                                                   #
# =========================================================================== #

def roms_get_time_index(ds, dstr, tvarname=None):
    """
    Return the 0-based integer index in ds closest to the requested time.

    Parameters
    ----------
    ds      : xarray.Dataset
    dstr    : int | float | str | np.datetime64 | pd.Timestamp
        Integer → returned as-is (fast path for callers that already know the index).
        'last' / 'latest' / 'end' → final time step.
        Strings are parsed as dates via pd.to_datetime.
        Floats are treated as numeric time values (compared directly to time_vals).
    tvarname : str, optional
        Name of the time coordinate. Auto-detected if omitted.

    Returns
    -------
    int
    """
    # Fast path: caller already knows the index
    if isinstance(dstr, (int, np.integer)):
        return int(dstr)

    if tvarname is None:
        for tn in ('time', 'ocean_time', 'bry_time', 'frc_time', 'sea_time'):
            if tn in ds.variables:
                tvarname = tn
                break
        else:
            raise ValueError('Unable to deduce time coordinate variable name.')

    time_vals = ds[tvarname].values

    if isinstance(dstr, str):
        if dstr.lower() in ('last', 'latest', 'end'):
            return len(time_vals) - 1
        req_dt64 = np.datetime64(pd.to_datetime(dstr))
    elif isinstance(dstr, (float, np.floating)):
        # Numeric time value — compare directly
        return int(np.abs(time_vals - dstr).argmin())
    elif isinstance(dstr, (np.datetime64, pd.Timestamp)):
        req_dt64 = np.datetime64(dstr)
    else:
        raise ValueError(f"Failed to parse requested time '{dstr}'")

    # Binary search on sorted time axis (O(log n) vs O(n) for argmin)
    idx = np.searchsorted(time_vals, req_dt64)
    idx = np.clip(idx, 1, len(time_vals) - 1)
    if abs(time_vals[idx - 1] - req_dt64) <= abs(time_vals[idx] - req_dt64):
        return idx - 1
    return int(idx)


# =========================================================================== #
#  Z-coordinate computation                                                     #
# =========================================================================== #

def precompute_roms_z_static(ds):
    """
    Precompute the time-invariant components of the Vtransform=2 z-coordinate.

    Call once per file (or per dataset). Pass the returned dict to
    compute_roms_z() for each time step.

    The z formula is:
        z = zeta * (1 + S_c) + h * S_c
    where S_c = (hc*s + h*Cs) / (hc + h).

    Factoring out the time-invariant parts:
        A = 1 + S_c   (multiplies zeta)
        B = h * S_c   (constant offset)
    so that compute_roms_z only needs two multiply-add operations per step.

    Parameters
    ----------
    ds : xarray.Dataset with variables h, hc (or attr), s_rho, Cs_r, s_w, Cs_w.

    Returns
    -------
    dict with keys A_r, B_r (shape N, eta, xi),
                   A_w, B_w (shape N+1, eta, xi),
                   has_zeta, zeta_dim.
    """
    vtransform = int(ds.attrs.get('Vtransform', 2))
    if 'Vtransform' in ds.variables:
        vtransform = int(ds['Vtransform'].values)
    if vtransform != 2:
        raise NotImplementedError(
            f'Vtransform={vtransform} is not supported; only Vtransform=2 is implemented.'
        )

    h  = ds['h'].values[np.newaxis, :, :]                      # (1, eta, xi)
    hc = (ds['hc'].values if 'hc' in ds.variables
          else float(ds.attrs.get('hc', 0.0)))

    s_rho = ds['s_rho'].values[:, np.newaxis, np.newaxis]       # (N,   1, 1)
    Cs_r  = ds['Cs_r'].values[:, np.newaxis, np.newaxis]
    s_w   = ds['s_w'].values[:, np.newaxis, np.newaxis]         # (N+1, 1, 1)
    Cs_w  = ds['Cs_w'].values[:, np.newaxis, np.newaxis]

    hc_plus_h = hc + h                                          # (1, eta, xi)
    S_c_r     = (hc * s_rho + h * Cs_r) / hc_plus_h
    S_c_w     = (hc * s_w   + h * Cs_w) / hc_plus_h

    return {
        'A_r':      1.0 + S_c_r,                               # (N,   eta, xi)
        'B_r':      h   * S_c_r,                               # (N,   eta, xi)
        'A_w':      1.0 + S_c_w,                               # (N+1, eta, xi)
        'B_w':      h   * S_c_w,                               # (N+1, eta, xi)
        'has_zeta': 'zeta' in ds.variables,
        'zeta_dim': ds['zeta'].dims[0] if 'zeta' in ds.variables else None,
    }


def compute_roms_z(ds, t_idx, static):
    """
    Compute z_r and z_w for a single time step.

    Parameters
    ----------
    ds     : xarray.Dataset
    t_idx  : int — time index (use roms_get_time_index to obtain)
    static : dict — output of precompute_roms_z_static(ds)

    Returns
    -------
    z_r : (N,   eta, xi) numpy array — rho-point depths (negative metres)
    z_w : (N+1, eta, xi) numpy array — w-point depths   (negative metres)
    """
    if static['has_zeta']:
        zeta = ds['zeta'].isel({static['zeta_dim']: t_idx}).values  # (eta, xi)
    else:
        zeta = np.zeros(static['B_r'].shape[1:], dtype=np.float64)

    zeta3d = zeta[np.newaxis, :, :]                             # (1, eta, xi)

    z_r = zeta3d * static['A_r'] + static['B_r']               # (N,   eta, xi)
    z_w = zeta3d * static['A_w'] + static['B_w']               # (N+1, eta, xi)

    return z_r, z_w


# =========================================================================== #
#  Mixed Layer Depth                                                            #
# =========================================================================== #

def roms_mld(ds, t_idx, z_r, opt='rho10m_plus_p125'):
    """
    Calculate Mixed Layer Depth using TEOS-10 (gsw).

    Criterion 'rho10m_plus_p125' (Aristizabal):
        MLD = shallowest depth where in-situ density exceeds the density at
        z = -10 m by more than 0.125 kg m⁻³.

    Parameters
    ----------
    ds    : xarray.Dataset with variables temp, salt, h, mask_rho, lon_rho, lat_rho.
    t_idx : int — time index.
    z_r   : (N, y, x) numpy array from compute_roms_z().
    opt   : str — criterion selector (only 'rho10m_plus_p125' / 'aristizabal' supported).

    Returns
    -------
    xr.DataArray, shape (y, x), units metres (positive down).
    """
    time_dim = ds['temp'].dims[0]
    temp_da  = ds['temp'].isel({time_dim: t_idx})
    temp     = temp_da.values
    salt     = ds['salt'].isel({time_dim: t_idx}).values
    np.clip(salt, 0.0, None, out=salt)                          # in-place, no mask overhead

    h         = ds['h'].values
    mask      = ds['mask_rho'].values
    lon       = ds['lon_rho'].values
    lat       = ds['lat_rho'].values

    # TEOS-10 density (fully vectorised)
    lat_3d = np.broadcast_to(lat, z_r.shape)                   # read-only view, no copy
    lon_3d = np.broadcast_to(lon, z_r.shape)
    p      = gsw.p_from_z(z_r, lat_3d)
    SA     = gsw.SA_from_SP(salt, p, lon_3d, lat_3d)
    CT     = gsw.CT_from_pt(SA, temp)
    rho    = gsw.rho(SA, CT, p)
    del SA, CT, p                                                # free ~2.9 GiB before delta_rho

    z_dim, y_dim, x_dim = rho.shape
    n_pts     = y_dim * x_dim
    rho2d     = rho.reshape(z_dim, n_pts)
    z_r_2d    = z_r.reshape(z_dim, n_pts)
    h_flat    = h.ravel()
    mask_flat = mask.ravel()
    pts       = np.arange(n_pts)

    if opt in ('rho10m_plus_p125', 'aristizabal'):
        target_z = -10.0
        Rvalue   = 0.125

        # --- Vectorised 10 m density interpolation ---
        n_below = np.sum(z_r_2d < target_z, axis=0)            # (n_pts,)
        k_lo    = np.clip(n_below - 1, 0, z_dim - 2)
        k_hi    = k_lo + 1

        z_lo = z_r_2d[k_lo, pts];  z_hi = z_r_2d[k_hi, pts]
        r_lo = rho2d[k_lo,  pts];  r_hi = rho2d[k_hi,  pts]
        dz   = z_hi - z_lo
        alpha  = np.where(dz != 0, (target_z - z_lo) / np.where(dz != 0, dz, 1.0), 0.0)
        rho10m = r_lo + alpha * (r_hi - r_lo)

        # Edge cases: target below seafloor or above surface
        rho10m = np.where(n_below == 0,     rho2d[0,         pts], rho10m)
        rho10m = np.where(n_below >= z_dim, rho2d[z_dim - 1, pts], rho10m)
        rho10m = np.where(mask_flat != 0,   rho10m,                np.nan)

        # --- Vectorised MLD threshold search ---
        delta_rho   = rho2d - rho10m[np.newaxis, :]            # (z_dim, n_pts)
        exceeds     = delta_rho > Rvalue
        has_exceeds = np.any(exceeds, axis=0)

        # Last (shallowest) index where delta_rho > Rvalue
        k      = (z_dim - 1) - np.argmax(exceeds[::-1, :], axis=0)
        k_safe = np.minimum(k, z_dim - 2)

        r0 = delta_rho[k_safe,     pts];  r1 = delta_rho[k_safe + 1, pts]
        z0 = z_r_2d[k_safe,        pts];  z1 = z_r_2d[k_safe + 1,    pts]
        dr = r1 - r0

        mld_interp = np.where(
            dr != 0,
            z0 + (Rvalue - r0) * (z1 - z0) / np.where(dr != 0, dr, 1.0),
            z0
        )
        mld_interp = np.where(k >= z_dim - 1, 0.0, mld_interp)

        mld_flat = np.where(
            mask_flat != 0,
            np.where(has_exceeds, mld_interp, -h_flat),
            np.nan
        )
    else:
        raise ValueError(f"Unknown MLD criterion '{opt}'")

    mld = -mld_flat.reshape(y_dim, x_dim)

    spatial_dims = temp_da.dims[-2:]
    valid_coords = {k: v for k, v in temp_da.coords.items()
                    if set(v.dims).issubset(spatial_dims)}

    return xr.DataArray(
        mld, dims=spatial_dims, coords=valid_coords, name='mld',
        attrs={'units': 'meter',
               'description': 'Mixed Layer Depth (rho > rho(-10m) + 0.125) via TEOS-10'}
    )


# =========================================================================== #
#  Ocean Heat Content                                                           #
# =========================================================================== #

def roms_ohc(ds, t_idx, z_r, z_w):
    """
    Calculate Ocean Heat Content integrated from the 26°C isotherm to the surface.

        OHC = rho0 * Cp * integral_{z(T=26)}^{0} (T - 26) dz   [kJ cm⁻²]

    Parameters
    ----------
    ds    : xarray.Dataset with variables temp, h, mask_rho.
    t_idx : int — time index.
    z_r   : (N,   y, x) numpy array from compute_roms_z().
    z_w   : (N+1, y, x) numpy array from compute_roms_z().

    Returns
    -------
    xr.DataArray, shape (y, x), units kJ cm⁻².
    """
    time_dim = ds['temp'].dims[0]
    temp_da  = ds['temp'].isel({time_dim: t_idx})
    data     = temp_da.values                                   # (N, y, x)

    z_dim, y_dim, x_dim = data.shape
    n_pts     = y_dim * x_dim
    data2d    = data.reshape(z_dim, n_pts)
    z_r_2d    = z_r.reshape(z_dim, n_pts)
    h_flat    = ds['h'].values.ravel()
    mask_flat = ds['mask_rho'].values.ravel()
    pts       = np.arange(n_pts)

    Tvalue = 26.0

    # --- Vectorised 26°C isotherm depth ---
    below_T   = data2d < Tvalue                                 # (N, n_pts)
    has_below = np.any(below_T, axis=0)

    # Last (shallowest) index where temp < 26°C
    k      = (z_dim - 1) - np.argmax(below_T[::-1, :], axis=0)
    k_safe = np.minimum(k, z_dim - 2)

    t0 = data2d[k_safe,     pts];  t1 = data2d[k_safe + 1, pts]
    z0 = z_r_2d[k_safe,     pts];  z1 = z_r_2d[k_safe + 1, pts]
    dt = t1 - t0

    zohc_interp = np.where(
        dt != 0,
        z0 + (Tvalue - t0) * (z1 - z0) / np.where(dt != 0, dt, 1.0),
        z0
    )
    zohc_interp = np.where(k >= z_dim - 1, 0.0, zohc_interp)  # column never reaches 26°C

    zohc_flat = np.where(
        mask_flat != 0,
        np.where(has_below, zohc_interp, -h_flat),             # all warm → integrate to bottom
        np.nan
    )
    zohc = zohc_flat.reshape(y_dim, x_dim)

    # --- Integrate (T - 26) from isotherm to surface ---
    zero_surf        = np.zeros_like(zohc)
    Tm26_int, _, _   = roms_zint(data - Tvalue, z_r, z_w, zohc, zero_surf)

    ohc = 1e-7 * 1025.0 * 3985.0 * Tm26_int

    spatial_dims = temp_da.dims[-2:]
    valid_coords = {k: v for k, v in temp_da.coords.items()
                    if set(v.dims).issubset(spatial_dims)}

    return xr.DataArray(
        ohc, dims=spatial_dims, coords=valid_coords,
        name='ocean_heat_content',
        attrs={'units': 'kJ/cm^2', 'description': 'Ocean Heat Content for T > 26C'}
    )


# =========================================================================== #
#  Vertical integration                                                         #
# =========================================================================== #

def roms_zint(data, z_r, z_w, *args):
    """
    Vertical integral of data (rho points) between depth surfaces.

    Parameters
    ----------
    data : array-like, shape (N, y, x)
        3-D field to integrate (xr.DataArray or numpy array).
    z_r  : array-like, shape (N, y, x)
        Rho-point depths (negative metres).
    z_w  : array-like, shape (N+1, y, x)
        W-point depths (negative metres). z_w[0] = -h by construction.
    *args controls integration bounds — three modes:
        ()           → full water column
        ([k2, k1],)  → between sigma level indices k2..k1  (0-based, k2 <= k1)
        (z2, z1)     → between depth surfaces z2 <= z1
                        (2-D arrays or scalars, both in negative metres;
                         z2 is the deeper/lower bound)

    Returns
    -------
    vint      : (y, x) ndarray — vertically integrated field
    thickness : (y, x) ndarray — thickness of the integrated layer
    dz_out    : (N_slice, y, x) ndarray — per-cell thicknesses used
                (full dz for modes 0/1; effective partial-cell dz for mode 2)

    Notes
    -----
    - Automatically interpolates z_r/z_w to u-grid (xi-averaged) or v-grid
      (eta-averaged) when the data spatial shape doesn't match the rho grid.
    - Land points (h = 0) integrate to zero without explicit masking.
    """
    # Normalise inputs
    if isinstance(data, xr.DataArray): data = data.values
    if isinstance(z_r,  xr.DataArray): z_r  = z_r.values
    if isinstance(z_w,  xr.DataArray): z_w  = z_w.values

    _, rz, cz = data.shape
    hrz, hcz  = z_w.shape[1], z_w.shape[2]

    # Staggered-grid interpolation
    if rz == hrz and cz == hcz:
        pass                                                    # rho grid
    elif rz == hrz and cz == hcz - 1:                          # u-grid (xi-averaged)
        z_w = 0.5 * (z_w[:, :, :-1] + z_w[:, :, 1:])
        z_r = 0.5 * (z_r[:, :, :-1] + z_r[:, :, 1:])
    elif rz == hrz - 1 and cz == hcz:                          # v-grid (eta-averaged)
        z_w = 0.5 * (z_w[:, :-1, :] + z_w[:, 1:, :])
        z_r = 0.5 * (z_r[:, :-1, :] + z_r[:, 1:, :])
    else:
        raise ValueError(
            f'data spatial shape ({rz}, {cz}) does not match '
            f'rho ({hrz}, {hcz}), u ({hrz}, {hcz-1}), or v ({hrz-1}, {hcz}) grids'
        )

    # ------------------------------------------------------------------ #
    #  Mode 0 — full water column                                          #
    # ------------------------------------------------------------------ #
    if len(args) == 0:
        dz        = np.diff(z_w, axis=0)
        vint      = np.sum(data * dz, axis=0)
        thickness = np.sum(dz, axis=0)
        return vint, thickness, dz

    # ------------------------------------------------------------------ #
    #  Mode 1 — between sigma level indices                               #
    # ------------------------------------------------------------------ #
    if len(args) == 1:
        z_r_indices = args[0]
        if len(z_r_indices) != 2:
            raise ValueError('z_r_indices must have exactly 2 elements [k2, k1]')
        k2, k1 = int(z_r_indices[0]), int(z_r_indices[1])
        dz = np.diff(z_w, axis=0)
        N  = dz.shape[0]
        if not (0 <= k2 <= k1 <= N - 1):
            raise ValueError(f'k2={k2}, k1={k1} out of valid range [0, {N - 1}]')
        dz_slice  = dz[k2:k1 + 1]
        vint      = np.sum(data[k2:k1 + 1] * dz_slice, axis=0)
        thickness = np.sum(dz_slice, axis=0)
        return vint, thickness, dz_slice

    # ------------------------------------------------------------------ #
    #  Mode 2 — between arbitrary depth surfaces                          #
    # ------------------------------------------------------------------ #
    if len(args) != 2:
        raise ValueError(
            f'Expected 0, 1, or 2 positional arguments after z_w; got {len(args)}'
        )

    z2, z1 = args
    z1 = np.asarray(z1.values if hasattr(z1, 'values') else z1, dtype=float)
    z2 = np.asarray(z2.values if hasattr(z2, 'values') else z2, dtype=float)

    if z1.ndim < 2 or z2.ndim < 2:
        if z1.size == 1 and z2.size == 1:
            z1 = np.full((rz, cz), float(z1))
            z2 = np.full((rz, cz), float(z2))
        else:
            raise ValueError('z1 and z2 must both be 2-D arrays or both scalars')

    if np.any(z2 > z1):
        raise ValueError(
            'z2 (lower/deeper bound) must be <= z1 (upper/shallower bound) '
            'everywhere. Check argument order: roms_zint(data, z_r, z_w, z2, z1)'
        )

    # Vectorised partial-cell integration.
    # eff_dz[k] = max(0,  min(z_w[k+1], z1)  -  max(z_w[k], z2))
    # Handles all cases in one broadcast: full cell, empty cell, partial bottom,
    # partial top, and both boundaries inside the same cell.
    z1_3d  = z1[np.newaxis, :, :]
    z2_3d  = z2[np.newaxis, :, :]
    eff_dz = np.maximum(
        np.minimum(z_w[1:], z1_3d) - np.maximum(z_w[:-1], z2_3d),
        0.0
    )                                                           # (N, rz, cz)

    vint      = np.sum(data * eff_dz, axis=0)
    real_bot  = np.maximum(z2, z_w[0])                         # clamp to seafloor
    real_top  = np.minimum(z1, z_w[-1])                        # clamp to surface
    thickness = real_top - real_bot

    return vint, thickness, eff_dz


# =========================================================================== #
#  Convenience wrappers (single-call, any context)                             #
# =========================================================================== #

def roms_mld_from_ds(ds, time_req, opt='rho10m_plus_p125'):
    """
    Convenience wrapper around roms_mld() — computes z_r internally.
    Use roms_mld() directly inside processing loops for better performance.
    """
    t_idx        = roms_get_time_index(ds, time_req)
    static       = precompute_roms_z_static(ds)
    z_r, _       = compute_roms_z(ds, t_idx, static)
    return roms_mld(ds, t_idx, z_r, opt=opt)


def roms_ohc_from_ds(ds, time_req):
    """
    Convenience wrapper around roms_ohc() — computes z_r/z_w internally.
    Use roms_ohc() directly inside processing loops for better performance.
    """
    t_idx        = roms_get_time_index(ds, time_req)
    static       = precompute_roms_z_static(ds)
    z_r, z_w     = compute_roms_z(ds, t_idx, static)
    return roms_ohc(ds, t_idx, z_r, z_w)
