#!/usr/bin/env python3
"""
CMIP6 hydrological data for the Chirchik River basin (Central Asia)
from the Copernicus Climate Data Store (CDS), dataset "projections-cmip6".

Outputs
  (1) SPATIAL : gridded NetCDF per model/experiment/variable for the basin
                area, plus a basin-average time series (area-weighted;
                clipped to your basin polygon if you supply one)
  (2) POINTS  : time series at station lat/lon (nearest grid cell or
                bilinear interpolation)

------------------------------------------------------------------------
ONE-TIME SETUP
  pip install "cdsapi>=0.7.2" xarray netCDF4 pandas numpy scipy
  optional, to clip to a basin polygon:  pip install geopandas regionmask

  1. Register at https://cds.climate.copernicus.eu
  2. Open the "CMIP6 climate projections" dataset -> Download tab ->
     accept the licence (requests fail until you do).
  3. Paste your personal access token into CDS_KEY below
     (no ~/.cdsapirc file needed).

TIP: parameter names below follow the CDS web form. If a request fails,
build the same request in the Download tab, click "Show API request"
and compare.
------------------------------------------------------------------------
"""

from pathlib import Path
import time
import zipfile

import numpy as np
import pandas as pd

# Safeguard: a broken "dask" install (version reported as 'unknown') crashes
# xarray with "InvalidVersion". This script does not need dask, so if it is
# broken we hide it from xarray. Must run BEFORE "import xarray".
try:
    import sys
    import dask
    from packaging.version import Version
    Version(dask.__version__)
except ImportError:
    pass                                   # dask not installed - fine
except Exception:
    for _m in [m for m in list(sys.modules) if m == "dask" or m.startswith("dask.")]:
        del sys.modules[_m]
    sys.modules["dask"] = None             # xarray now treats dask as absent
    print("Note: broken dask installation detected - running without dask.")

import xarray as xr

# =====================================================================
# INPUT / OUTPUT PATHS - edit these first
# (use forward slashes "/" on Windows, or prefix the string with r"...")
# =====================================================================

PROJECT_DIR = Path("C:/Users/jt231697/Downloads/CMIP6/Data")

# ---- inputs ----
INPUT_DIR    = PROJECT_DIR / "Input"
STATIONS_CSV = INPUT_DIR / "stations.csv"      # columns: name, lat, lon
                                               # (example file created if missing)
BASIN_FILE   = None                            # optional basin polygon, e.g.
                                               # INPUT_DIR / "chirchik_basin.shp"

# ---- outputs ----
OUT         = PROJECT_DIR / "Output"
RAW_DIR     = OUT / "raw_zip"      # zip files as downloaded from the CDS
NC_DIR      = OUT / "netcdf"       # unzipped NetCDF files
SPATIAL_DIR = OUT / "spatial"      # (1) gridded basin NetCDF per model/scenario
POINT_DIR   = OUT / "points"       # (2) station time series CSVs
# Output CSVs, one per experiment (all variables as columns):
#   OUT / basin_mean_<resolution>_<experiment>_<models>.csv
#   POINT_DIR / stations_<resolution>_<experiment>_<models>.csv
#   POINT_DIR / station_grid_cells_<resolution>_<models>.csv
#   POINT_DIR / by_station/<station>/<station>_<resolution>_<experiment>_<models>.csv

# =====================================================================
# CDS CREDENTIALS - paste your personal access token here
# (CDS website -> your profile -> "API Token"). Keep this script private:
# anyone with the token can use your CDS account.
# =====================================================================

CDS_URL = "https://cds.climate.copernicus.eu/api"
CDS_KEY = ""

# =====================================================================
# CONFIGURATION - edit this block
# =====================================================================

# CDS variable name -> CMIP6 short name inside the NetCDF files
VARIABLES = {
    "precipitation": "pr",                                         # kg m-2 s-1
    "total_runoff": "mrro",                                        # kg m-2 s-1
    # "evaporation_including_sublimation_and_transpiration": "evspsbl",
    # "moisture_in_upper_portion_of_soil_column": "mrsos",         # kg m-2
    # "surface_snow_amount": "snw",                                # kg m-2 (SWE)
    # "snowfall_flux": "prsn",                                     # kg m-2 s-1
    "near_surface_air_temperature": "tas",                         # K
}

# CDS model names (lower case, underscores). Not every model provides every
# variable/scenario - unavailable combinations are skipped and logged.
# MODELS = ["mpi_esm1_2_lr", "ec_earth3", "ipsl_cm6a_lr", "miroc6", "noresm2_mm"]
MODELS = ["miroc6"]

# experiment -> (first year, last year)
EXPERIMENTS = {
    "historical": (1981, 2014),
    "ssp2_4_5": (2015, 2100),
    "ssp5_8_5": (2015, 2100),
}

TEMPORAL_RESOLUTION = "monthly"   # "monthly" or "daily"
# Daily data: models with 360-day or noleap calendars are handled exactly
# (dates such as 1981-02-30 are kept as they are in the model).
YEARS_PER_REQUEST = 100 if TEMPORAL_RESOLUTION == "monthly" else 10

# Bounding box [North, West, South, East].
# The Chirchik basin (Pskem, Chatkal, Ugam -> Charvak -> Syr Darya) lies
# roughly within 40.8-42.4 N, 68.8-71.6 E. The box is padded because GCM
# cells are 1-2.5 degrees wide; a tight box can return zero cells.
BBOX = [43.5, 67.5, 39.5, 72.5]

POINT_METHOD = "nearest"          # "nearest" or "linear" (bilinear)

# also write one CSV per station (POINT_DIR / by_station / <station> / ...)
PER_STATION_FILES = True

CONVERT_UNITS = True              # fluxes -> mm/day, temperature -> degC

# Resuming: just run the script again. Finished downloads are kept, broken or
# half-finished ones are deleted and requested again.
MAX_TRIES = 2                     # attempts per request (temporary errors)
RETRY_WAIT = 60                   # seconds to wait before retrying

FLUX_VARS = {"pr", "mrro", "mrros", "evspsbl", "prsn"}

# =====================================================================
# 1. DOWNLOAD
# =====================================================================

def build_request(cds_var, experiment, model, y0, y1):
    req = {
        "temporal_resolution": TEMPORAL_RESOLUTION,
        "experiment": experiment,
        "variable": cds_var,
        "model": model,
        "year": [str(y) for y in range(y0, y1 + 1)],
        "month": [f"{m:02d}" for m in range(1, 13)],
        "area": BBOX,
        "download_format": "zip",
    }
    if TEMPORAL_RESOLUTION == "monthly":
        req["level"] = "single_levels"
    else:
        req["day"] = [f"{d:02d}" for d in range(1, 32)]
    return req


def nc_folder(model, experiment, short):
    # monthly and daily files go to separate folders so they are never mixed
    return NC_DIR / TEMPORAL_RESOLUTION / model / experiment / short


def model_tag():
    """Model names joined, used in output file names (e.g. 'miroc6')."""
    return "_".join(MODELS)


def zip_ok(path):
    """True if the file is a complete, readable zip containing NetCDF."""
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.testzip() is None and any(n.endswith(".nc") for n in zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


def unzip_nc(zip_path, dest):
    """Extract NetCDF files, skipping ones already extracted completely."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if not info.filename.endswith(".nc"):
                continue
            out = dest / Path(info.filename).name
            if out.exists() and out.stat().st_size == info.file_size:
                continue
            with zf.open(info) as src, open(out, "wb") as dst:
                dst.write(src.read())


FATAL_WORDS = ("licence", "license", "401", "403", "unauthorized", "forbidden")


def download_all():
    import cdsapi

    if "PASTE-YOUR" in CDS_KEY:
        raise SystemExit("Set CDS_KEY at the top of the script first.")
    client = cdsapi.Client(url=CDS_URL, key=CDS_KEY)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    failed = []
    n_have = n_new = 0

    for model in MODELS:
        for exp, (y0, y1) in EXPERIMENTS.items():
            for cds_var, short in VARIABLES.items():
                for c0 in range(y0, y1 + 1, YEARS_PER_REQUEST):
                    c1 = min(c0 + YEARS_PER_REQUEST - 1, y1)
                    tag = f"{short}_{TEMPORAL_RESOLUTION}_{exp}_{model}_{c0}-{c1}"
                    target = RAW_DIR / f"{tag}.zip"
                    part = RAW_DIR / f"{tag}.zip.part"   # download in progress

                    if part.exists():
                        part.unlink()                     # leftover from a crash

                    # --- already downloaded? -------------------------------
                    if target.exists():
                        if zip_ok(target):
                            n_have += 1
                            unzip_nc(target, nc_folder(model, exp, short))
                            continue
                        print(f"Broken file, downloading again: {target.name}")
                        target.unlink()

                    # --- request it (with retries) -------------------------
                    for attempt in range(1, MAX_TRIES + 1):
                        print(f"Requesting {tag} (attempt {attempt}/{MAX_TRIES}) ...")
                        try:
                            client.retrieve("projections-cmip6",
                                            build_request(cds_var, exp, model, c0, c1),
                                            str(part))
                            if not zip_ok(part):
                                raise RuntimeError("downloaded file is not a valid zip")
                            part.replace(target)          # only now counts as done
                            unzip_nc(target, nc_folder(model, exp, short))
                            n_new += 1
                            break
                        except Exception as err:
                            msg = str(err)
                            if part.exists():
                                part.unlink()
                            if any(w in msg.lower() for w in FATAL_WORDS):
                                raise SystemExit(
                                    "Stopped: login/licence problem - fix it and run "
                                    f"again (finished files are kept).\n{msg[:500]}")
                            print(f"  FAILED: {msg[:300]}")
                            if attempt < MAX_TRIES:
                                print(f"  retrying in {RETRY_WAIT} s ...")
                                time.sleep(RETRY_WAIT)
                            else:
                                failed.append({"request": tag, "error": msg[:300]})

    print(f"\nDownload summary: {n_have} already present, {n_new} new, "
          f"{len(failed)} failed")
    f = OUT / f"failed_requests_{model_tag()}.csv"
    if failed:
        OUT.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(failed).to_csv(f, index=False)
        print(f"Failed requests listed in {f} - run the script again to retry them.")
    elif f.exists():
        f.unlink()                                        # old list is out of date


# =====================================================================
# 2. READ + PREPARE
# =====================================================================

def load_variable(model, experiment, short):
    """Concatenate all NetCDF files for one model/experiment/variable."""
    files = sorted(nc_folder(model, experiment, short).rglob("*.nc"))
    if not files:
        return None
    parts = [xr.open_dataset(f)[short] for f in files]
    da = xr.concat(parts, dim="time") if len(parts) > 1 else parts[0]
    da = da.sortby("time")
    _, keep = np.unique(da["time"].values, return_index=True)
    da = da.isel(time=np.sort(keep))
    lat = da["lat"].values
    if lat[0] > lat[-1]:
        da = da.sortby("lat")
    return da.load()


def convert_units(da, short):
    if not CONVERT_UNITS:
        return da
    attrs = dict(da.attrs)
    units = attrs.get("units", "")
    if short in FLUX_VARS and units == "kg m-2 s-1":
        da = da * 86400.0
        attrs["units"] = "mm day-1"
    elif short == "tas" and units == "K":
        da = da - 273.15
        attrs["units"] = "degC"
    elif units == "kg m-2":          # snw, mrsos: 1 kg m-2 == 1 mm water
        attrs["units"] = "mm"
    da.attrs = attrs
    return da


def time_strings(da):
    # monthly data -> "YYYY-MM" so all variables line up on the same rows.
    # cftime keeps non-standard calendars exact (e.g. 30 Feb in 360-day models).
    fmt = "%Y-%m" if TEMPORAL_RESOLUTION == "monthly" else "%Y-%m-%d"
    t = da["time"].values
    if np.issubdtype(t.dtype, np.datetime64):
        return pd.to_datetime(t).strftime(fmt).tolist()
    return [x.strftime(fmt) for x in t]

# =====================================================================
# 3. SPATIAL PRODUCTS
# =====================================================================

def basin_weights(da):
    """Area weights on the model grid: cos(lat), times the fraction of each
    cell inside the basin polygon if BASIN_FILE is given."""
    w = np.cos(np.deg2rad(da["lat"])).broadcast_like(da.isel(time=0, drop=True))
    if BASIN_FILE is None:
        return w

    import geopandas as gpd
    import regionmask

    basin = gpd.read_file(BASIN_FILE).to_crs("EPSG:4326").dissolve()
    regions = regionmask.from_geopandas(basin)
    try:
        # fractional overlap - important for coarse GCM cells
        frac = regions.mask_3D_frac_approx(da.lon, da.lat).isel(region=0, drop=True)
    except Exception:
        frac = regions.mask_3D(da.lon, da.lat).isel(region=0, drop=True).astype(float)
    if float(frac.sum()) == 0:
        print("  WARNING: no grid cell overlaps the basin; using bounding box.")
        return w
    return w * frac


def spatial_outputs(da, short, model, exp):
    SPATIAL_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{short}_{TEMPORAL_RESOLUTION}_{model}_{exp}"

    weights = basin_weights(da)
    gridded = da.where(weights > 0)          # cells outside basin -> NaN
    gridded.to_dataset(name=short).to_netcdf(SPATIAL_DIR / f"{stem}.nc")

    mean = da.weighted(weights.fillna(0)).mean(("lat", "lon"))
    return pd.DataFrame({
        "time": time_strings(da),
        "variable": short,
        "units": da.attrs.get("units", ""),
        "model": model,
        "experiment": exp,
        "basin_mean": mean.values,
        "n_cells": int((weights > 0).sum()),
    })

# =====================================================================
# 4. POINT (STATION) PRODUCTS
# =====================================================================

def load_stations():
    path = Path(STATIONS_CSV)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # EXAMPLE coordinates only - replace with your gauge/met stations
        pd.DataFrame({
            "name": ["Tashkent", "Charvak_area", "Pskem_valley", "Chatkal_valley"],
            "lat": [41.33, 41.62, 41.90, 41.80],
            "lon": [69.30, 70.05, 70.35, 70.60],
        }).to_csv(path, index=False)
        print(f"Created example {path} - edit it with your own stations.")
    return pd.read_csv(path)


def point_outputs(da, short, model, exp, stations):
    lons = stations["lon"].to_numpy(dtype=float)
    if float(da["lon"].max()) > 180:
        lons = lons % 360
    lat_pts = xr.DataArray(stations["lat"].to_numpy(dtype=float), dims="station")
    lon_pts = xr.DataArray(lons, dims="station")

    if POINT_METHOD == "linear":
        pts = da.interp(lat=lat_pts, lon=lon_pts)
        glat, glon = stations["lat"].to_numpy(), lons
    else:
        pts = da.sel(lat=lat_pts, lon=lon_pts, method="nearest")
        glat, glon = pts["lat"].values, pts["lon"].values

    times = time_strings(da)
    frames = []
    for i, row in stations.reset_index(drop=True).iterrows():
        frames.append(pd.DataFrame({
            "time": times,
            "station": row["name"],
            "station_lat": row["lat"],
            "station_lon": row["lon"],
            "grid_lat": float(glat[i]),
            "grid_lon": float(glon[i]),
            "variable": short,
            "units": da.attrs.get("units", ""),
            "model": model,
            "experiment": exp,
            "value": pts.isel(station=i).values,
        }))
    return pd.concat(frames, ignore_index=True)

# =====================================================================
# MAIN
# =====================================================================

def safe_name(name):
    """Station name usable as a Windows folder/file name."""
    return "".join(c if (c.isalnum() or c in "_-") else "_" for c in str(name))


def process_all():
    OUT.mkdir(parents=True, exist_ok=True)
    tag = model_tag()
    stations = load_stations()
    basin_tables, point_tables = [], []

    for model in MODELS:
        for exp in EXPERIMENTS:
            for short in VARIABLES.values():
                da = load_variable(model, exp, short)
                if da is None:
                    continue
                print(f"Processing {short} {model} {exp}: {da.sizes['time']} steps, "
                      f"{da.sizes['lat']}x{da.sizes['lon']} cells")
                da = convert_units(da, short)
                basin_tables.append(spatial_outputs(da, short, model, exp))
                point_tables.append(point_outputs(da, short, model, exp, stations))

    if not basin_tables:
        print("No NetCDF files found - nothing to process.")
        return

    basin_all = pd.concat(basin_tables, ignore_index=True)
    pts_all = pd.concat(point_tables, ignore_index=True)
    POINT_DIR.mkdir(parents=True, exist_ok=True)

    # column name = variable + units, e.g. "pr [mm day-1]"
    basin_all["column"] = basin_all["variable"] + " [" + basin_all["units"] + "]"
    pts_all["column"] = pts_all["variable"] + " [" + pts_all["units"] + "]"
    col_order = list(dict.fromkeys(basin_all["column"]))   # keep variable order

    # ---- one CSV per experiment ----------------------------------------
    for exp in EXPERIMENTS:

        # (1) basin mean: rows = time (x model), columns = variables
        b = basin_all[basin_all["experiment"] == exp]
        if len(b):
            wide = (b.set_index(["time", "model", "column"])["basin_mean"]
                     .unstack("column").reset_index())
            wide = wide[["time", "model"] + [c for c in col_order if c in wide]]
            wide = wide.sort_values(["model", "time"])
            f = OUT / f"basin_mean_{TEMPORAL_RESOLUTION}_{exp}_{tag}.csv"
            wide.to_csv(f, index=False)
            print(f"Wrote {f}")

        # (2) stations: rows = time x station (x model), columns = variables
        p = pts_all[pts_all["experiment"] == exp]
        if len(p):
            ids = ["time", "station", "station_lat", "station_lon", "model"]
            wide = (p.set_index(ids + ["column"])["value"]
                     .unstack("column").reset_index())
            wide = wide[ids + [c for c in col_order if c in wide]]
            wide = wide.sort_values(["model", "station", "time"])
            f = POINT_DIR / f"stations_{TEMPORAL_RESOLUTION}_{exp}_{tag}.csv"
            wide.to_csv(f, index=False)
            print(f"Wrote {f}")

            # (3) optional: one file per station
            if PER_STATION_FILES:
                for name, grp in wide.groupby("station", sort=False):
                    safe = safe_name(name)
                    sdir = POINT_DIR / "by_station" / safe
                    sdir.mkdir(parents=True, exist_ok=True)
                    grp.to_csv(sdir / f"{safe}_{TEMPORAL_RESOLUTION}_{exp}_{tag}.csv",
                               index=False)
                print(f"Wrote per-station files to {POINT_DIR / 'by_station'}")

    # which model grid cell each station was taken from
    cells = pts_all[["station", "station_lat", "station_lon", "model",
                     "variable", "grid_lat", "grid_lon"]].drop_duplicates()
    cells.to_csv(POINT_DIR / f"station_grid_cells_{TEMPORAL_RESOLUTION}_{tag}.csv",
                 index=False)

    print(f"Done. Outputs in {OUT.resolve()}")


if __name__ == "__main__":
    download_all()
    process_all()