# =====================================================================
# CMIP6 hydrological data for the Chirchik River basin (Central Asia)
# from the Copernicus Climate Data Store (CDS), dataset "projections-cmip6".
#
# Outputs
#   (1) SPATIAL : gridded NetCDF per model/experiment/variable for the basin
#                 area, plus a basin-average time series (area-weighted by
#                 cell fraction inside your basin polygon, if supplied)
#   (2) POINTS  : time series at station lat/lon (nearest cell or bilinear)
#
# ---------------------------------------------------------------------
# ONE-TIME SETUP
#   install.packages(c("ecmwfr", "terra"))      # ecmwfr >= 2.0
#
#   1. Register at https://cds.climate.copernicus.eu
#   2. Open "CMIP6 climate projections" -> Download tab -> accept the
#      licence (requests fail until you do).
#   3. Paste your personal access token into cds_key below.
#
# TIP: parameter names follow the CDS web form. If a request fails, build
# the same request in the Download tab, click "Show API request" and compare.
# =====================================================================

library(ecmwfr)
library(terra)
library(ncdf4)

# =====================================================================
# INPUT / OUTPUT PATHS - edit these first
# (use forward slashes "/" on Windows)
# =====================================================================

project_dir <- "C:/Users/jt2316/CMIP6_R"

# ---- inputs ----
input_dir    <- file.path(project_dir, "Input")
stations_csv <- file.path(input_dir, "stations.csv")   # columns: name, lat, lon
# (example file created if missing)
basin_file   <- NULL                                   # optional basin polygon, e.g.
# file.path(input_dir, "chirchik_basin.shp")

# ---- outputs ----
out_dir     <- file.path(project_dir, "Output")
raw_dir     <- file.path(out_dir, "raw_zip")   # zip files as downloaded from the CDS
nc_dir      <- file.path(out_dir, "netcdf")    # unzipped NetCDF files
spatial_dir <- file.path(out_dir, "spatial")   # (1) gridded basin NetCDF per model/scenario
point_dir   <- file.path(out_dir, "points")    # (2) station time series CSVs
# Output CSVs, one per experiment (all variables as columns):
#   Output/basin_mean_<resolution>_<experiment>_<models>.csv
#   Output/points/stations_<resolution>_<experiment>_<models>.csv
#   Output/points/station_grid_cells_<resolution>_<models>.csv

# =====================================================================
# CDS CREDENTIALS - paste your personal access token here
# (CDS website -> your profile -> "API Token"). Keep this script private:
# anyone with the token can use your CDS account.
# =====================================================================

cds_url <- "https://cds.climate.copernicus.eu/api"   # ecmwfr >= 2.0 uses this by default
cds_key <- "xxxxxxxxxxxxx"

if (grepl("PASTE-YOUR", cds_key)) stop("Set cds_key at the top of the script first.")
wf_set_key(key = cds_key)   # stores the token in your system keyring

# =====================================================================
# CONFIGURATION
# =====================================================================

# CDS variable name = CMIP6 short name inside the NetCDF files
variables <- c(
  precipitation                                       = "pr",         # kg m-2 s-1
  total_runoff                                        = "mrro",       # kg m-2 s-1
  #  evaporation_including_sublimation_and_transpiration = "evspsbl",
  #  moisture_in_upper_portion_of_soil_column            = "mrsos",   # kg m-2
  #  surface_snow_amount                                 = "snw",     # kg m-2 (SWE)
  #  snowfall_flux                                       = "prsn",    # kg m-2 s-1
  near_surface_air_temperature                        = "tas"         # K
)

# Not every model provides every variable/scenario; failures are logged.
#models <- c("mpi_esm1_2_lr", "ec_earth3", "ipsl_cm6a_lr", "miroc6", "noresm2_mm")
models <- c("miroc6")

experiments <- list(
  historical = c(1981, 2014),
  ssp2_4_5   = c(2015, 2100),
  ssp5_8_5   = c(2015, 2100)
)

temporal_resolution <- "monthly"             # "monthly" or "daily"
# NOTE for daily data: models with a 360-day calendar (e.g. hadgem3_gc31_ll,
# ukesm1_0_ll, kace_1_0_g) have dates like 30 February, which R cannot store;
# their daily dates come out approximate. Prefer standard/noleap-calendar
# models for daily work in R (MIROC6, MPI, IPSL, NorESM2, EC-Earth3 are fine).
years_per_request   <- if (temporal_resolution == "monthly") 100 else 10

# Bounding box c(North, West, South, East). The Chirchik basin lies roughly
# within 40.8-42.4 N, 68.8-71.6 E; the box is padded because GCM cells are
# 1-2.5 degrees wide and a tight box can return no cells.
bbox <- c(43.5, 67.5, 39.5, 72.5)

point_method <- "simple"    # "simple" (nearest cell) or "bilinear"

# also write one CSV per station (Output/points/by_station/<station>/...)
per_station_files <- TRUE

convert_units <- TRUE       # fluxes -> mm/day, temperature -> degC

# Resuming: just run the script again. Finished downloads are kept, broken or
# half-finished ones are deleted and requested again.
max_tries  <- 2             # attempts per request (temporary errors)
retry_wait <- 60            # seconds to wait before retrying

flux_vars <- c("pr", "mrro", "mrros", "evspsbl", "prsn")

# =====================================================================
# 1. DOWNLOAD
# =====================================================================

build_request <- function(cds_var, experiment, model, y0, y1, target) {
  req <- list(
    dataset_short_name  = "projections-cmip6",
    temporal_resolution = temporal_resolution,
    experiment          = experiment,
    variable            = cds_var,
    model               = model,
    year                = as.character(y0:y1),
    month               = sprintf("%02d", 1:12),
    area                = bbox,
    download_format     = "zip",
    target              = target
  )
  if (temporal_resolution == "monthly") {
    req$level <- "single_levels"
  } else {
    req$day <- sprintf("%02d", 1:31)
  }
  req
}

# monthly and daily files go to separate folders so they are never mixed
nc_folder <- function(model, experiment, short) {
  file.path(nc_dir, temporal_resolution, model, experiment, short)
}

zip_ok <- function(path) {
  # TRUE if the file is a complete, readable zip containing NetCDF
  if (!file.exists(path) || file.size(path) == 0) return(FALSE)
  lst <- tryCatch(unzip(path, list = TRUE), error = function(e) NULL,
                  warning = function(w) NULL)
  !is.null(lst) && any(grepl("\\.nc$", lst$Name))
}

unzip_nc <- function(zip_path, dest) {
  # extract NetCDF files, skipping ones already extracted completely
  dir.create(dest, recursive = TRUE, showWarnings = FALSE)
  lst <- unzip(zip_path, list = TRUE)
  lst <- lst[grepl("\\.nc$", lst$Name), ]
  out <- file.path(dest, basename(lst$Name))
  todo <- !file.exists(out) | file.size(out) != lst$Length
  if (any(todo)) unzip(zip_path, files = lst$Name[todo], exdir = dest, junkpaths = TRUE)
}

fatal_words <- "licen[cs]e|401|403|unauthori[sz]ed|forbidden"

download_all <- function() {
  dir.create(raw_dir, recursive = TRUE, showWarnings = FALSE)
  model_tag <- paste(models, collapse = "_")
  failed <- data.frame(request = character(), error = character())
  n_have <- 0; n_new <- 0
  
  for (model in models) {
    for (exp in names(experiments)) {
      yrs <- experiments[[exp]]
      for (cds_var in names(variables)) {
        short <- variables[[cds_var]]
        for (c0 in seq(yrs[1], yrs[2], by = years_per_request)) {
          c1   <- min(c0 + years_per_request - 1, yrs[2])
          tag  <- sprintf("%s_%s_%s_%s_%d-%d", short, temporal_resolution,
                          exp, model, c0, c1)
          zip_path  <- file.path(raw_dir, paste0(tag, ".zip"))
          part_name <- paste0(tag, ".zip.part")            # download in progress
          part_path <- file.path(raw_dir, part_name)
          dest      <- nc_folder(model, exp, short)
          
          if (file.exists(part_path)) file.remove(part_path)   # leftover from a crash
          
          # --- already downloaded? -----------------------------------
          if (file.exists(zip_path)) {
            if (zip_ok(zip_path)) {
              n_have <- n_have + 1
              unzip_nc(zip_path, dest)
              next
            }
            message("Broken file, downloading again: ", basename(zip_path))
            file.remove(zip_path)
          }
          
          # --- request it (with retries) -----------------------------
          for (attempt in seq_len(max_tries)) {
            message(sprintf("Requesting %s (attempt %d/%d) ...", tag, attempt, max_tries))
            err <- tryCatch({
              wf_request(
                request  = build_request(cds_var, exp, model, c0, c1, part_name),
                transfer = TRUE,
                path     = raw_dir,
                time_out = 3 * 3600
              )
              if (!zip_ok(part_path)) stop("downloaded file is not a valid zip")
              file.rename(part_path, zip_path)               # only now counts as done
              unzip_nc(zip_path, dest)
              NULL
            }, error = function(e) conditionMessage(e))
            
            if (is.null(err)) { n_new <- n_new + 1; break }
            
            if (file.exists(part_path)) file.remove(part_path)
            if (grepl(fatal_words, err, ignore.case = TRUE)) {
              stop("Stopped: login/licence problem - fix it and run again ",
                   "(finished files are kept).\n", substr(err, 1, 500), call. = FALSE)
            }
            message("  FAILED: ", substr(err, 1, 300))
            if (attempt < max_tries) {
              message("  retrying in ", retry_wait, " s ...")
              Sys.sleep(retry_wait)
            } else {
              failed[nrow(failed) + 1, ] <- c(tag, substr(err, 1, 300))
            }
          }
        }
      }
    }
  }
  
  message(sprintf("\nDownload summary: %d already present, %d new, %d failed",
                  n_have, n_new, nrow(failed)))
  f <- file.path(out_dir, paste0("failed_requests_", model_tag, ".csv"))
  if (nrow(failed) > 0) {
    dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
    write.csv(failed, f, row.names = FALSE)
    message("Failed requests listed in ", f, " - run the script again to retry them.")
  } else if (file.exists(f)) {
    file.remove(f)                                      # old list is out of date
  }
  invisible(NULL)
}

# =====================================================================
# 2. READ + PREPARE
# =====================================================================

load_variable <- function(model, experiment, short) {
  files <- sort(list.files(nc_folder(model, experiment, short),
                           pattern = "\\.nc$", full.names = TRUE,
                           recursive = TRUE))
  if (length(files) == 0) return(NULL)
  
  r <- do.call(c, lapply(files, function(f) rast(f, subds = short)))
  if (crs(r) == "") crs(r) <- "EPSG:4326"
  
  # files are named by date range (e.g. ..._19810101-19901231.nc), so the
  # sorted file order is already chronological. No re-sorting by date:
  # models with a 360-day calendar would get scrambled.
  r
}

do_convert_units <- function(r, short) {
  u <- units(r)[1]
  if (!convert_units) return(list(r = r, units = u))
  tt <- time(r)
  if (short %in% flux_vars && u == "kg m-2 s-1") {
    r <- r * 86400; u <- "mm day-1"
  } else if (short == "tas" && u == "K") {
    r <- r - 273.15; u <- "degC"
  } else if (u == "kg m-2") {
    u <- "mm"                                   # 1 kg m-2 == 1 mm water
  }
  time(r) <- tt
  units(r) <- u
  list(r = r, units = u)
}

time_strings <- function(r) {
  tt <- time(r)
  if (all(is.na(tt))) return(names(r))
  # monthly data -> "YYYY-MM" so all variables line up on the same rows
  fmt <- if (temporal_resolution == "monthly") "%Y-%m" else "%Y-%m-%d"
  format(as.Date(tt), fmt)
}

# =====================================================================
# 3. SPATIAL PRODUCTS
# =====================================================================

basin <- NULL
if (!is.null(basin_file)) {
  basin <- aggregate(project(vect(basin_file), "EPSG:4326"))
}

spatial_outputs <- function(r, u, short, model, exp) {
  dir.create(spatial_dir, recursive = TRUE, showWarnings = FALSE)
  stem <- sprintf("%s_%s_%s_%s", short, temporal_resolution, model, exp)
  
  if (!is.null(basin)) {
    # keep every cell touching the basin (coarse GCM grid)
    gridded <- mask(crop(r, basin, snap = "out"), basin, touches = TRUE)
    # area-weighted basin mean: weights = fraction of each cell inside polygon
    m <- extract(r, basin, fun = mean, weights = TRUE, na.rm = TRUE, ID = FALSE)
    basin_mean <- as.numeric(m[1, ])
    n_cells <- nrow(extract(r[[1]], basin, touches = TRUE, ID = FALSE))
  } else {
    gridded <- r
    w <- init(r[[1]], "y")                      # cos(latitude) weights
    w <- cos(w * pi / 180)
    basin_mean <- as.numeric(global(r * w, "sum", na.rm = TRUE)[, 1] /
                               global(mask(w, r[[1]]), "sum", na.rm = TRUE)[, 1])
    n_cells <- ncell(r)
  }
  
  time(gridded) <- time(r)
  writeCDF(gridded, file.path(spatial_dir, paste0(stem, ".nc")),
           varname = short, unit = u, overwrite = TRUE)
  
  data.frame(time = time_strings(r), variable = short, units = u,
             model = model, experiment = exp,
             basin_mean = basin_mean, n_cells = n_cells)
}

# =====================================================================
# 4. POINT (STATION) PRODUCTS
# =====================================================================

load_stations <- function() {
  if (!file.exists(stations_csv)) {
    dir.create(dirname(stations_csv), recursive = TRUE, showWarnings = FALSE)
    # EXAMPLE coordinates only - replace with your gauge/met stations
    write.csv(data.frame(
      name = c("Tashkent", "Charvak", "Pskem", "Chatkal"),
      lat  = c(41.33, 41.62, 41.90, 41.80),
      lon  = c(69.30, 70.05, 70.35, 70.60)
    ), stations_csv, row.names = FALSE)
    message("Created example ", stations_csv, " - edit it with your own stations.")
  }
  read.csv(stations_csv)
}

point_outputs <- function(r, u, short, model, exp, st) {
  lon <- st$lon
  if (xmax(r) > 180) lon <- lon %% 360
  pts <- vect(data.frame(lon = lon, lat = st$lat), geom = c("lon", "lat"),
              crs = "EPSG:4326")
  
  vals <- extract(r, pts, method = point_method, ID = FALSE)   # stations x time
  cell_xy <- unname(xyFromCell(r, cellFromXY(r, crds(pts))))
  times <- time_strings(r)
  
  do.call(rbind, lapply(seq_len(nrow(st)), function(i) {
    data.frame(time = times, station = st$name[i],
               station_lat = st$lat[i], station_lon = st$lon[i],
               grid_lat = cell_xy[i, 2], grid_lon = cell_xy[i, 1],
               variable = short, units = u, model = model, experiment = exp,
               value = as.numeric(vals[i, ]))
  }))
}

# =====================================================================
# MAIN
# =====================================================================

process_all <- function() {
  dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
  model_tag <- paste(models, collapse = "_")   # used in output file names
  st <- load_stations()
  basin_tabs <- list(); point_tabs <- list()
  
  for (model in models) {
    for (exp in names(experiments)) {
      for (short in unname(variables)) {
        r <- load_variable(model, exp, short)
        if (is.null(r)) next
        message(sprintf("Processing %s %s %s: %d steps, %dx%d cells",
                        short, model, exp, nlyr(r), nrow(r), ncol(r)))
        cu <- do_convert_units(r, short)
        key <- paste(short, model, exp)
        basin_tabs[[key]] <- spatial_outputs(cu$r, cu$units, short, model, exp)
        point_tabs[[key]] <- point_outputs(cu$r, cu$units, short, model, exp, st)
      }
    }
  }
  if (length(basin_tabs) == 0) {
    message("No NetCDF files found - nothing to process.")
    return(invisible(NULL))
  }
  
  basin_all <- do.call(rbind, basin_tabs)
  pts_all   <- do.call(rbind, point_tabs)
  rownames(basin_all) <- NULL; rownames(pts_all) <- NULL
  dir.create(point_dir, recursive = TRUE, showWarnings = FALSE)
  
  # column name = variable + units, e.g. "pr [mm day-1]"
  basin_all$column <- paste0(basin_all$variable, " [", basin_all$units, "]")
  pts_all$column   <- paste0(pts_all$variable,   " [", pts_all$units,   "]")
  
  # ---- one CSV per experiment ----------------------------------------
  for (exp in names(experiments)) {
    
    # (1) basin mean: rows = time (x model), columns = variables
    b <- basin_all[basin_all$experiment == exp,
                   c("time", "model", "column", "basin_mean")]
    if (nrow(b) > 0) {
      wide <- reshape(b, idvar = c("time", "model"), timevar = "column",
                      direction = "wide")
      names(wide) <- sub("^basin_mean\\.", "", names(wide))
      wide <- wide[order(wide$model, wide$time), ]
      f <- file.path(out_dir, sprintf("basin_mean_%s_%s_%s.csv", temporal_resolution, exp, model_tag))
      write.csv(wide, f, row.names = FALSE)
      message("Wrote ", f)
    }
    
    # (2) stations: rows = time x station (x model), columns = variables
    p <- pts_all[pts_all$experiment == exp,
                 c("time", "station", "station_lat", "station_lon",
                   "model", "column", "value")]
    if (nrow(p) > 0) {
      wide <- reshape(p, idvar = c("time", "station", "station_lat",
                                   "station_lon", "model"),
                      timevar = "column", direction = "wide")
      names(wide) <- sub("^value\\.", "", names(wide))
      wide <- wide[order(wide$model, wide$station, wide$time), ]
      f <- file.path(point_dir, sprintf("stations_%s_%s_%s.csv", temporal_resolution, exp, model_tag))
      write.csv(wide, f, row.names = FALSE)
      message("Wrote ", f)
      
      # (3) optional: one file per station
      if (per_station_files) {
        for (s in unique(wide$station)) {
          safe <- gsub("[^A-Za-z0-9_-]", "_", s)          # safe folder/file name
          sdir <- file.path(point_dir, "by_station", safe)
          dir.create(sdir, recursive = TRUE, showWarnings = FALSE)
          write.csv(wide[wide$station == s, ],
                    file.path(sdir, sprintf("%s_%s_%s_%s.csv", safe,
                                            temporal_resolution, exp, model_tag)),
                    row.names = FALSE)
        }
        message("Wrote per-station files to ", file.path(point_dir, "by_station"))
      }
    }
  }
  
  # which model grid cell each station was taken from
  cells <- unique(pts_all[, c("station", "station_lat", "station_lon", "model",
                              "variable", "grid_lat", "grid_lon")])
  write.csv(cells, file.path(point_dir,
                             sprintf("station_grid_cells_%s_%s.csv", temporal_resolution, model_tag)),
            row.names = FALSE)
  
  message("Done. Outputs in ", normalizePath(out_dir))
}

download_all()
process_all()
