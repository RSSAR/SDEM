# SDEM

```text
   ███████╗██████╗ ███████╗███╗   ███╗
   ██╔════╝██╔══██╗██╔════╝████╗ ████║
   ███████╗██║  ██║█████╗  ██╔████╔██║
   ╚════██║██║  ██║██╔══╝  ██║╚██╔╝██║
   ███████║██████╔╝███████╗██║ ╚═╝ ██║
   ╚══════╝╚═════╝ ╚══════╝╚═╝     ╚═╝

            Fast SAR DEM Downloader
              Powered by sardem + aria2
```

**SDEM** is a lightweight SAR-oriented DEM downloader for **Sentinel-1** and **NISAR** workflows. It automatically determines the SAR footprint, adds a configurable buffer, downloads the required **Copernicus GLO-30** tiles in parallel with `aria2c`, builds a persistent local VRT cache, and delegates DEM grid alignment, nodata handling, resampling, and vertical-datum conversion to [`sardem`](https://github.com/scottstanie/sardem).

Developer: **Shuai Wang**  
Affiliation: **China University of Mining and Technology**

## Why SDEM?

`sardem` already contains the DEM-processing logic needed for InSAR. SDEM intentionally does **not** reimplement that science. Instead, it adds two conveniences around the `sardem` Copernicus backend:

1. Automatic footprint extraction from Sentinel-1 SAFE/ZIP products and NISAR RSLC HDF5 products.
2. Parallel whole-tile prefetching with `aria2c` and a persistent local Copernicus cache.

The processing chain is therefore:

```text
Sentinel-1 SAFE / ZIP       NISAR RSLC HDF5
          \                    /
           \                  /
            ---- footprint ----
                   |
                   v
             buffered bbox
                   |
                   v
        Copernicus GLO-30 tiles
                   |
                   v
          aria2 parallel download
                   |
                   v
          persistent local cache
                   |
                   v
              local VRT
                   |
                   v
       sardem Copernicus backend
             /           \
            v             v
         ISCE2           ISCE3
       dem.wgs84         dem.tif
```

## Current validation status

- **Sentinel-1 -> ISCE2:** tested with a 35-scene Sentinel-1 stack over Portuguese Bend, California.
- **NISAR RSLC -> ISCE3:** input detection and GeoTIFF output workflow are implemented; additional scene-by-scene validation is encouraged.
- Copernicus GLO-30 vertical handling is performed by `sardem`, using its EGM2008-to-WGS84 ellipsoidal-height workflow.

## Requirements

Recommended platform: Linux / WSL / Linux server.

Core requirements:

- Python 3.10+
- GDAL + PROJ
- `aria2c`
- `sardem == 0.13.0`
- `h5py`
- `shapely`
- `requests`

### Recommended Conda installation

```bash
conda env create -f environment.yml
conda activate sdem
```

Or install manually:

```bash
conda install -c conda-forge gdal aria2 h5py shapely requests
python -m pip install sardem==0.13.0
```

Check the installation:

```bash
python sdem.py --version
python sdem.py -h
aria2c --version
gdalinfo --version
```

## Quick start

### Sentinel-1 -> ISCE2

If `../SLC` contains Sentinel-1 `.SAFE` directories and/or Sentinel-1 ZIP products:

```bash
python sdem.py ../SLC
```

Auto mode detects Sentinel-1 and writes an ISCE2-ready DEM, normally:

```text
dem.wgs84
dem.hdr
dem.wgs84.xml
```

The exact GDAL ENVI sidecar filename follows GDAL's ENVI driver conventions.

### NISAR RSLC -> ISCE3

If `../RSLC` contains NISAR RSLC `.h5` products:

```bash
python sdem.py ../RSLC
```

Auto mode detects NISAR RSLC and writes:

```text
dem.tif
```

### Explicit output mode

```bash
python sdem.py ../SLC --format isce2
python sdem.py ../RSLC --format isce3
python sdem.py ../SLC --format both
```

### Explicit bounding box

Bounding-box order is:

```text
LEFT BOTTOM RIGHT TOP
```

Example:

```bash
python sdem.py --bbox -118.43 33.71 -118.34 33.80
```

With an explicit bbox, `auto` defaults to ISCE3 GeoTIFF because no sensor identity is available.

### DEM buffer

The default buffer is `0.2°` on all sides:

```bash
python sdem.py ../RSLC --buffer 0.1
```

### aria2 parallelism

Defaults:

- concurrent tiles: `8`
- connections per tile: `4`

Example:

```bash
python sdem.py ../RSLC --aria2-jobs 16 --aria2-connections 4
```

### Build only the local VRT

```bash
python sdem.py ../RSLC --vrt-only
```

This downloads/reuses the needed Copernicus tiles and prints the local VRT path without generating the final DEM.

## How footprint detection works

### Sentinel-1

SDEM scans Sentinel-1 SAFE directories and ZIP products and reads annotation geolocation-grid coordinates to determine the union extent.

### NISAR

SDEM opens RSLC HDF5 products in metadata-only mode and reads:

```text
/science/LSAR/identification/productType
/science/LSAR/identification/boundingPolygon
```

The complex SAR arrays are not loaded into memory for footprint detection.

For custom subset HDF5 products, the result assumes that `boundingPolygon` was updated consistently by the subsetting workflow. An explicit `--bbox` can always be used to override metadata.

## Local cache

Default cache location:

```text
~/.cache/sdem/cop30/
```

Typical layout:

```text
~/.cache/sdem/cop30/
├── tileList.txt
├── tiles/
│   └── Copernicus_DSM_COG_10_*.tif
└── vrts/
    └── cop30_*.vrt
```

Already downloaded tiles are reused on subsequent runs.

To refresh the cached Copernicus tile list:

```bash
python sdem.py ../SLC --refresh-tile-list
```

## DEM processing and vertical datum

SDEM uses a local Copernicus VRT as the input to `sardem.cop_dem.download_and_stitch()`. The final Copernicus processing remains inside `sardem`, including:

- source grid alignment;
- 1-arc-second output spacing at the default rate;
- nearest-neighbor resampling at native resolution;
- `srcNodata=0` / `dstNodata=0` handling;
- EGM2008 to WGS84 ellipsoidal-height conversion for InSAR processing.

This design avoids maintaining a separate DEM-science implementation in SDEM.

## Command-line help

```bash
python sdem.py -h
```

Version:

```bash
python sdem.py --version
```

## Limitations

- SDEM currently focuses on Copernicus GLO-30.
- First-time runs download complete intersecting COG tiles. For a very small AOI, direct remote COG range access may transfer less data; SDEM is most useful when stable parallel downloading and repeated tile reuse matter.
- A working GDAL/PROJ installation is required for `sardem` vertical-datum processing.
- NISAR subset products rely on correct `boundingPolygon` metadata unless `--bbox` is supplied.
- The current release has been validated most extensively for Sentinel-1/ISCE2; broader NISAR/ISCE3 testing is welcome.

## Attribution

SDEM uses [`sardem`](https://github.com/scottstanie/sardem) for final DEM processing. `sardem` is developed by Scott Staniewicz and is distributed under the MIT License.

Copernicus GLO-30 DEM tiles are accessed from the Copernicus DEM public AWS dataset.

See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for third-party notices.

## Citation

If you use SDEM in research, please cite this repository. GitHub citation metadata are provided in [`CITATION.cff`](CITATION.cff).

## License

SDEM is released under the MIT License. See [`LICENSE`](LICENSE).
