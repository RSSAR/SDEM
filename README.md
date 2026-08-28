# SDEM

```text
   ███████╗██████╗ ███████╗███╗   ███╗
   ██╔════╝██╔══██╗██╔════╝████╗ ████║
   ███████╗██║  ██║█████╗  ██╔████╔██║
   ╚════██║██║  ██║██╔══╝  ██║╚██╔╝██║
   ███████║██████╔╝███████╗██║ ╚═╝ ██║
   ╚══════╝╚═════╝ ╚══════╝╚═╝     ╚═╝

            SAR-aware DEM Downloader
               Powered by sardem
```

**SDEM** is a lightweight SAR-aware wrapper around [`sardem`](https://github.com/scottstanie/sardem) for **Sentinel-1** and **NISAR** workflows.

SDEM does not reimplement DEM science. Its role is to understand SAR products, determine the required geographic extent, choose an appropriate `sardem` DEM source, and select a practical output format for the downstream InSAR workflow.

Developer: **Shuai Wang**  
Affiliation: **China University of Mining and Technology**

## What SDEM adds to sardem

`sardem` already provides the DEM download, grid generation, reprojection, resampling and vertical-datum processing. SDEM adds the SAR-aware layer around it:

1. Automatic footprint extraction from Sentinel-1 SAFE/ZIP products.
2. Automatic footprint extraction from NISAR RSLC HDF5 products.
3. Automatic DEM-source selection.
4. Automatic ISCE2 / ISCE3 output-mode selection.
5. Optional aria2 acceleration and persistent caching for the Copernicus (`COP`) source.
6. Safe aria2 resume handling after interrupted Copernicus tile downloads.

The default processing architecture is:

```text
                    SAR products
                         |
               automatic footprint
                         |
                    buffered bbox
                         |
             automatic DEM-source choice
                  /                 \
                 /                   \
        Sentinel-1                  NISAR RSLC
            |                           |
            v                           v
       sardem COP                  sardem NISAR
            |                           |
     optional aria2                      |
      local cache                        |
            |                           |
            v                           v
         ISCE2                        ISCE3
      dem.wgs84                      dem.tif
```

In other words:

```text
Sentinel-1 -> COP
NISAR      -> NISAR
```

unless you explicitly override the source with `--dem-source`.

## Version 1.1.0

SDEM v1.1.0 changes the NISAR architecture substantially.

Earlier SDEM versions detected NISAR RSLC footprints but still downloaded Copernicus GLO-30 tiles through the SDEM aria2 cache. Starting with v1.1.0, NISAR RSLC products use the **native sardem NISAR DEM backend by default**.

Therefore, running:

```bash
python sdem.py ../RSLC
```

now behaves conceptually like:

```text
NISAR RSLC HDF5
      |
      v
SDEM extracts bbox
      |
      v
sardem data_source=NISAR
      |
      v
dem.tif
```

The Copernicus + aria2 path is still available and remains the default for Sentinel-1.

## Requirements

Recommended platform: Linux / WSL / Linux server.

Core requirements:

- Python 3.10+
- GDAL + PROJ
- `sardem >= 0.13.0`
- `h5py`
- `numpy`
- `shapely`
- `requests`

Optional but recommended for the Copernicus backend:

- `aria2c`

### Recommended Conda installation

```bash
conda env create -f environment.yml
conda activate sdem
```

Or install manually:

```bash
conda install -c conda-forge gdal aria2 h5py numpy shapely requests
python -m pip install -U sardem
```

Check the installation:

```bash
python sdem.py --version
python sdem.py -h
aria2c --version
gdalinfo --version
```

## Quick start

### Sentinel-1 -> COP -> ISCE2

If `../SLC` contains Sentinel-1 `.SAFE` directories and/or ZIP products:

```bash
python sdem.py ../SLC
```

Auto mode selects:

```text
SAR type    : Sentinel-1
DEM source  : COP
Output      : ISCE2
```

Typical output:

```text
dem.wgs84
dem.hdr
dem.wgs84.xml
```

For the `COP` source, SDEM uses aria2 by default to download complete intersecting Copernicus tiles into a persistent cache and supplies the validated local VRT to sardem.

### NISAR RSLC -> NISAR DEM -> ISCE3

If `../RSLC` contains NISAR RSLC `.h5` products:

```bash
python sdem.py ../RSLC
```

Auto mode selects:

```text
SAR type    : NISAR RSLC
DEM source  : NISAR
Output      : ISCE3
```

Typical output:

```text
dem.tif
```

For this path, SDEM does **not** download Copernicus tiles with aria2. The bbox is passed directly to the native sardem NISAR backend.

## DEM-source selection

Use:

```bash
--dem-source auto|cop|nisar|3dep|nasa
```

Default:

```text
auto
```

Automatic selection is:

```text
Sentinel-1 -> COP
NISAR      -> NISAR
```

### Force Copernicus for NISAR

This is useful for comparisons or environments where the sardem NISAR backend is unavailable:

```bash
python sdem.py ../RSLC --dem-source cop
```

That command intentionally uses the Copernicus + aria2 path.

### Force NISAR DEM source

```bash
python sdem.py ../RSLC --dem-source nisar
```

### Other sardem sources

For an explicit bbox, other sardem-supported sources may also be selected, for example:

```bash
python sdem.py \
    --bbox -118.43 33.71 -118.34 33.80 \
    --dem-source 3dep
```

Availability depends on the installed sardem version and source coverage.

## Output modes

Use:

```bash
--format auto|isce2|isce3|both
```

Automatic selection is:

```text
Sentinel-1 -> ISCE2
NISAR      -> ISCE3
```

Examples:

```bash
python sdem.py ../SLC --format isce3
python sdem.py ../RSLC --format isce2
python sdem.py ../RSLC --format both
```

When `both` is selected, SDEM creates the GeoTIFF once and translates it to ISCE2 ENVI format rather than downloading and processing the DEM twice.

## Explicit bounding box

Bounding-box order is:

```text
LEFT BOTTOM RIGHT TOP
```

Example:

```bash
python sdem.py --bbox -118.43 33.71 -118.34 33.80
```

When only `--bbox` is supplied, no SAR sensor identity is available. Therefore:

```text
DEM source auto -> COP
format auto     -> ISCE3
```

You can override either choice explicitly.

## DEM buffer

The default buffer is `0.2°` on all sides:

```bash
python sdem.py ../RSLC --buffer 0.1
```

## How footprint detection works

### Sentinel-1

SDEM recursively scans Sentinel-1 SAFE directories and ZIP products and reads annotation geolocation-grid coordinates. Multiple products are merged to obtain the stack union extent.

### NISAR

SDEM opens RSLC HDF5 metadata and first attempts to read:

```text
/science/LSAR/identification/boundingPolygon
```

RSLC identity is determined using the available product metadata, `/science/LSAR/RSLC` structure, and standard NISAR RSLC filename signals.

For subset or repacked RSLC products where some identification metadata have been removed, SDEM includes a fallback using the retained RSLC geolocation grid when it contains usable longitude/latitude coordinates.

The complex SAR image arrays are not loaded for normal footprint extraction.

## Copernicus aria2 acceleration

aria2 acceleration is used **only when the selected DEM source is `COP`**.

Defaults:

```text
Concurrent tiles      : 8
Connections per tile  : 4
```

Example:

```bash
python sdem.py ../SLC \
    --aria2-jobs 8 \
    --aria2-connections 4
```

For a NISAR-native run such as:

```bash
python sdem.py ../RSLC
```

these options are not used because the selected source is `NISAR`.

### Disable aria2 for COP

To bypass SDEM's local Copernicus cache and let sardem use its normal remote COP VRT directly:

```bash
python sdem.py ../SLC --no-aria2
```

## Interrupted downloads and resume support

For the Copernicus aria2 path, SDEM supports safe restart after an interrupted download.

aria2 normally leaves a pair such as:

```text
Copernicus_DSM_....tif
Copernicus_DSM_....tif.aria2
```

The `.aria2` sidecar means the TIFF is incomplete but resumable.

SDEM therefore follows this logic:

```text
.tif.aria2 exists
       |
       v
keep partial .tif
       |
       v
aria2 --continue=true
       |
       v
resume download
       |
       v
full raster validation
```

It does not treat an incomplete TIFF as a valid cache simply because GDAL can open its header.

After download completion, SDEM performs a deeper GDAL read/checksum validation so truncated compressed TIFF blocks are detected before the tile is used to build a VRT.

## Local Copernicus cache

The cache applies only to the SDEM `COP` + aria2 path.

Default location:

```text
~/.cache/sdem/cop30/
```

Typical layout:

```text
~/.cache/sdem/cop30/
├── tileList.txt
├── tiles/
│   ├── Copernicus_DSM_COG_10_*.tif
│   └── Copernicus_DSM_COG_10_*.tif.aria2   # only while incomplete
└── vrts/
    └── cop30_*.vrt
```

Completed, validated tiles are reused on later runs.

To refresh the public Copernicus tile list:

```bash
python sdem.py ../SLC --refresh-tile-list
```

## Build only the local COP VRT

`--vrt-only` applies only to the Copernicus + aria2 workflow:

```bash
python sdem.py ../SLC --dem-source cop --vrt-only
```

It downloads/resumes/validates the required Copernicus tiles, builds the local VRT, prints the path, and exits without creating the final DEM.

## DEM processing responsibility

SDEM intentionally limits its scientific responsibility.

SDEM handles:

- SAR-product discovery;
- Sentinel-1 / NISAR footprint extraction;
- bbox buffering;
- DEM-source selection;
- output-format selection;
- optional Copernicus aria2 caching and validation.

`sardem` handles the actual DEM processing, including the source-specific download/access logic, output grid generation, reprojection/resampling and relevant vertical-datum handling.

This keeps SDEM as a thin, maintainable **SAR-aware wrapper around sardem** rather than a competing DEM implementation.

## Command-line help

```bash
python sdem.py -h
```

Version:

```bash
python sdem.py --version
```

Current release:

```text
SDEM 1.1.0
```

## Notes and limitations

- The available DEM sources depend on the installed sardem version and source coverage.
- The native NISAR DEM backend may require the authentication/configuration expected by sardem for that data source.
- `aria2c` is optional for native NISAR/3DEP/NASA backends but required when using SDEM's accelerated `COP` path.
- A working GDAL/PROJ installation is required for the supported output workflows.
- NISAR subset/repacked products must retain enough footprint/geolocation metadata for automatic bbox extraction; `--bbox` can always be used as an override.

## Attribution

SDEM uses [`sardem`](https://github.com/scottstanie/sardem) for DEM processing and native DEM-source access. `sardem` is developed by Scott Staniewicz and distributed under the MIT License.

For the accelerated `COP` path, Copernicus GLO-30 tiles are accessed from the public Copernicus DEM AWS dataset.

See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for third-party notices.

## Citation

If you use SDEM in research, please cite this repository. GitHub citation metadata are provided in [`CITATION.cff`](CITATION.cff).

## License

SDEM is released under the MIT License. See [`LICENSE`](LICENSE).
