#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sdem.py - fast SAR DEM downloader powered by sardem + aria2.

The script intentionally leaves DEM science/geometry processing to sardem.
It only adds two conveniences:

1. Auto-detect a lon/lat extent from Sentinel-1 SAFE/ZIP or NISAR RSLC HDF5.
2. Prefetch the required Copernicus GLO-30 COG tiles with aria2c into a
   persistent local cache, build a local VRT, then pass that VRT to sardem.

Typical use
-----------
    python sdem.py ../SLC

If ../SLC contains Sentinel-1 SAFE/ZIP products, output defaults to an ISCE2
DEM (dem.wgs84 + XML). If it contains NISAR RSLC .h5 products, output defaults
to an ISCE3 GeoTIFF (dem.tif).

You can override detection with:
    --format isce2 | isce3 | both

All pixel-grid alignment, resampling, nodata handling, and vertical-datum
processing remain inside sardem.

SDEM v1.1.1 fixes sardem 0.13.0 API compatibility:
- NISAR/3DEP/NASA use sardem.dem.main() without vrt_filename.
- COP + SDEM aria2 local VRT uses sardem.cop_dem.download_and_stitch(),
  where vrt_filename is actually supported.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple
from xml.etree import ElementTree as ET

import requests

LOG = logging.getLogger("sdem")

COP30_BASE_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
COP30_TILE_LIST_URL = f"{COP30_BASE_URL}/tileList.txt"
COP30_RES = 1.0 / 3600.0
COP30_HALF_PIXEL = 0.5 * COP30_RES

BBox = Tuple[float, float, float, float]  # left, bottom, right, top
SDEM_VERSION = "1.1.1"

BANNER = r"""
   ███████╗██████╗ ███████╗███╗   ███╗
   ██╔════╝██╔══██╗██╔════╝████╗ ████║
   ███████╗██║  ██║█████╗  ██╔████╔██║
   ╚════██║██║  ██║██╔══╝  ██║╚██╔╝██║
   ███████║██████╝ ███████╗██║ ╚═╝ ██║
   ╚══════╝╚═════╝  ╚══════╝╚═╝     ╚═╝

            SAR-aware DEM Downloader
               Powered by sardem

   Developer : Shuai Wang
   Affiliation: China University of Mining and Technology
"""

ASCII_BANNER = r"""
   S D E M
   SAR-aware DEM Downloader
   Powered by sardem

   Developer : Shuai Wang
   Affiliation: China University of Mining and Technology
"""


def print_banner() -> None:
    try:
        print(BANNER)
    except UnicodeEncodeError:
        print(ASCII_BANNER)



def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _get_gdal():
    try:
        from osgeo import gdal
    except ImportError as exc:
        raise RuntimeError(
            "GDAL Python bindings are required. Install GDAL in the same environment "
            "as sardem (for example via conda-forge)."
        ) from exc
    gdal.UseExceptions()
    return gdal


def _get_sardem_utils():
    try:
        from sardem import utils
    except ImportError as exc:
        raise RuntimeError("sardem is required: python -m pip install -U sardem") from exc
    return utils


def _get_sardem_dem():
    try:
        from sardem import dem
    except ImportError as exc:
        raise RuntimeError("sardem is required: python -m pip install -U sardem") from exc
    return dem


def _get_sardem_cop_dem():
    """Return sardem's Copernicus backend.

    sardem 0.13.0 exposes ``vrt_filename`` on
    ``sardem.cop_dem.download_and_stitch`` but not on ``sardem.dem.main``.
    """
    try:
        from sardem import cop_dem
    except ImportError as exc:
        raise RuntimeError("sardem is required: python -m pip install -U sardem") from exc
    return cop_dem


def _ensure_sardem_source(source: str) -> None:
    try:
        from sardem.download import Downloader
    except ImportError as exc:
        raise RuntimeError("sardem is required: python -m pip install -U sardem") from exc

    valid = {str(x).upper() for x in Downloader.VALID_SOURCES}
    if source.upper() not in valid:
        raise RuntimeError(
            f"Installed sardem does not support data source {source!r}. "
            "Upgrade sardem; NISAR support requires sardem >= 0.13.0."
        )


def _default_cache_dir() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "sdem" / "cop30"


# -----------------------------------------------------------------------------
# Copernicus tile prefetch
# -----------------------------------------------------------------------------
def _tile_name(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return (
        f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_"
        f"{ew}{abs(lon):03d}_00_DEM"
    )


def _split_dateline(bbox: BBox) -> List[BBox]:
    utils = _get_sardem_utils()
    return [tuple(map(float, b)) for b in utils.check_dateline(bbox)]


def _aligned_bbox(bbox: BBox) -> BBox:
    # Deliberately use sardem's exact pixel-edge alignment semantics.
    utils = _get_sardem_utils()
    return tuple(map(float, utils.align_bounds_to_pixel_grid(bbox)))


def _candidate_tile_origins(bbox: BBox) -> Iterable[Tuple[int, int]]:
    """Yield integer SW tile origins whose pixel area intersects bbox."""
    left, bottom, right, top = _aligned_bbox(bbox)

    lon0 = math.floor(left) - 1
    lon1 = math.ceil(right) + 1
    lat0 = math.floor(bottom) - 1
    lat1 = math.ceil(top) + 1

    for lat in range(max(-90, lat0), min(89, lat1) + 1):
        tile_bottom = lat - COP30_HALF_PIXEL
        tile_top = lat + 1.0 - COP30_HALF_PIXEL
        if tile_top <= bottom or tile_bottom >= top:
            continue
        for lon in range(max(-180, lon0), min(179, lon1) + 1):
            tile_left = lon - COP30_HALF_PIXEL
            tile_right = lon + 1.0 - COP30_HALF_PIXEL
            if tile_right <= left or tile_left >= right:
                continue
            yield lat, lon


def _read_tile_list(
    cache_dir: Path, refresh: bool = False, timeout: int = 60
) -> set[str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    tile_list_file = cache_dir / "tileList.txt"
    if refresh or not tile_list_file.exists() or tile_list_file.stat().st_size == 0:
        LOG.info("Fetching Copernicus tile list")
        r = requests.get(COP30_TILE_LIST_URL, timeout=timeout)
        r.raise_for_status()
        tile_list_file.write_bytes(r.content)

    names = {
        line.strip().strip("\r")
        for line in tile_list_file.read_text(
            encoding="utf-8", errors="ignore"
        ).splitlines()
        if line.strip()
    }
    if not names:
        raise RuntimeError(f"Copernicus tile list is empty: {tile_list_file}")
    return names


def required_tile_names(
    bbox: BBox, cache_dir: Path, refresh_tile_list: bool = False
) -> List[str]:
    available = _read_tile_list(cache_dir, refresh=refresh_tile_list)
    names: set[str] = set()
    for sub_bbox in _split_dateline(bbox):
        for lat, lon in _candidate_tile_origins(sub_bbox):
            name = _tile_name(lat, lon)
            if name in available:
                names.add(name)
            else:
                # Missing GLO-30 tiles are expected over ocean.
                LOG.debug("No public COP30 tile: %s", name)
    return sorted(names)


def _tile_path(cache_dir: Path, name: str) -> Path:
    return cache_dir / "tiles" / f"{name}.tif"


def _tile_url(name: str) -> str:
    return f"{COP30_BASE_URL}/{name}/{name}.tif"


def _validate_raster(path: Path) -> bool:
    if not path.exists() or path.stat().st_size <= 0:
        return False
    try:
        gdal = _get_gdal()
        ds = gdal.Open(str(path), gdal.GA_ReadOnly)
        ok = ds is not None and ds.RasterXSize > 0 and ds.RasterYSize > 0
        ds = None
        return bool(ok)
    except Exception:
        return False


def prefetch_tiles(
    names: Sequence[str],
    cache_dir: Path | None = None,
    jobs: int = 8,
    connections_per_file: int = 4,
    retries: int = 5,
    timeout: int = 60,
) -> List[Path]:
    """Download missing Copernicus COGs with one aria2c batch process."""
    cache_dir = Path(cache_dir or _default_cache_dir()).expanduser().resolve()
    tiles_dir = cache_dir / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)

    aria2 = shutil.which("aria2c")
    if not aria2:
        raise RuntimeError("aria2c not found. Install it, e.g. sudo apt install aria2")

    valid: List[Path] = []
    missing: List[Tuple[str, Path]] = []
    for name in names:
        path = _tile_path(cache_dir, name)
        if _validate_raster(path):
            valid.append(path)
        else:
            if path.exists():
                path.unlink(missing_ok=True)
            missing.append((name, path))

    if missing:
        LOG.info(
            "aria2: downloading %d COP30 tiles (%d cached)", len(missing), len(valid)
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".aria2.txt", delete=False
        ) as f:
            input_file = Path(f.name)
            for name, path in missing:
                f.write(_tile_url(name) + "\n")
                f.write(f"  dir={path.parent}\n")
                f.write(f"  out={path.name}\n")

        cmd = [
            aria2,
            f"--input-file={input_file}",
            f"--max-concurrent-downloads={max(1, jobs)}",
            f"--max-connection-per-server={max(1, connections_per_file)}",
            f"--split={max(1, connections_per_file)}",
            "--min-split-size=1M",
            "--continue=true",
            "--file-allocation=none",
            "--auto-file-renaming=false",
            "--allow-overwrite=true",
            f"--max-tries={max(1, retries)}",
            "--retry-wait=2",
            f"--timeout={max(10, timeout)}",
            "--summary-interval=1",
            "--console-log-level=notice",
        ]
        try:
            subprocess.run(cmd, check=True)
        finally:
            input_file.unlink(missing_ok=True)

    paths: List[Path] = []
    bad: List[str] = []
    for name in names:
        path = _tile_path(cache_dir, name)
        if _validate_raster(path):
            paths.append(path)
        else:
            bad.append(name)
    if bad:
        raise RuntimeError(
            "Downloaded COP30 tiles failed validation: " + ", ".join(bad[:10])
        )
    return paths


def build_local_vrt(tile_paths: Sequence[Path], vrt_path: Path) -> Path:
    """Build a local VRT for sardem, preserving Copernicus EGM2008 CRS."""
    gdal = _get_gdal()
    vrt_path = Path(vrt_path).resolve()
    vrt_path.parent.mkdir(parents=True, exist_ok=True)
    if not tile_paths:
        raise RuntimeError("No public Copernicus tiles intersect the requested bbox")

    # Match sardem's Copernicus VRT semantics: GLO-30 is WGS84 horizontal
    # coordinates with EGM2008 orthometric heights.
    opts = gdal.BuildVRTOptions(
        resampleAlg="nearest",
        resolution="highest",
        outputSRS="EPSG:4326+3855",
    )
    ds = gdal.BuildVRT(str(vrt_path), [str(p) for p in tile_paths], options=opts)
    if ds is None:
        raise RuntimeError("gdal.BuildVRT failed")
    ds.FlushCache()
    ds = None
    return vrt_path


def prepare_local_vrt(
    bbox: BBox,
    cache_dir: Path | None = None,
    jobs: int = 8,
    connections_per_file: int = 4,
    refresh_tile_list: bool = False,
    vrt_path: Path | None = None,
) -> Path:
    cache_dir = Path(cache_dir or _default_cache_dir()).expanduser().resolve()
    names = required_tile_names(
        bbox, cache_dir, refresh_tile_list=refresh_tile_list
    )
    LOG.info("COP30 tiles intersecting bbox: %d", len(names))
    paths = prefetch_tiles(
        names,
        cache_dir=cache_dir,
        jobs=jobs,
        connections_per_file=connections_per_file,
    )
    if vrt_path is None:
        key = hashlib.sha1("|".join(names).encode()).hexdigest()[:12]
        vrt_path = cache_dir / "vrts" / f"cop30_{key}.vrt"
    return build_local_vrt(paths, Path(vrt_path))


# -----------------------------------------------------------------------------
# SAR footprint readers
# -----------------------------------------------------------------------------
def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _robust_lonlat_extent(lons: Sequence[float], lats: Sequence[float]) -> BBox:
    if not lons or not lats:
        raise ValueError("No valid longitude/latitude coordinates")

    min_lat = min(lats)
    max_lat = max(lats)
    span_native = max(lons) - min(lons)

    lons360 = [lon if lon >= 0.0 else lon + 360.0 for lon in lons]
    span360 = max(lons360) - min(lons360)

    if span360 < span_native and span360 < 180.0:
        left360 = min(lons360)
        right360 = max(lons360)
        left = left360 if left360 <= 180.0 else left360 - 360.0
        right = right360 if right360 <= 180.0 else right360 - 360.0
        return float(left), float(min_lat), float(right), float(max_lat)

    return float(min(lons)), float(min_lat), float(max(lons)), float(max_lat)


def _merge_extents(extents: Sequence[BBox]) -> BBox:
    if not extents:
        raise ValueError("No extents to merge")
    lons: List[float] = []
    lats: List[float] = []
    for left, bottom, right, top in extents:
        lons.extend([left, right])
        lats.extend([bottom, top])
    return _robust_lonlat_extent(lons, lats)


def _parse_s1_annotation_xml(xml_bytes: bytes) -> Tuple[List[float], List[float]]:
    root = ET.fromstring(xml_bytes)
    lons: List[float] = []
    lats: List[float] = []

    for elem in root.iter():
        if _local_name(elem.tag) != "geolocationGridPoint":
            continue
        lat = None
        lon = None
        for child in elem:
            name = _local_name(child.tag)
            if name == "latitude":
                lat = child.text
            elif name == "longitude":
                lon = child.text
        if lat is None or lon is None:
            continue
        try:
            la = float(lat)
            lo = float(lon)
        except (TypeError, ValueError):
            continue
        if -90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0:
            lats.append(la)
            lons.append(lo)

    return lons, lats


def _sentinel_extent(product: Path) -> BBox:
    lons_all: List[float] = []
    lats_all: List[float] = []

    if product.is_dir() and product.name.upper().endswith(".SAFE"):
        ann = product / "annotation"
        xml_files = sorted(ann.glob("*.xml")) if ann.exists() else []
        if not xml_files:
            xml_files = sorted(product.rglob("annotation/*.xml"))
        for xml_file in xml_files:
            try:
                lons, lats = _parse_s1_annotation_xml(xml_file.read_bytes())
                lons_all.extend(lons)
                lats_all.extend(lats)
            except Exception as exc:
                LOG.debug("Skipping annotation %s: %s", xml_file, exc)

    elif product.is_file() and product.suffix.lower() == ".zip":
        with zipfile.ZipFile(product, "r") as zf:
            names = [
                n
                for n in zf.namelist()
                if "/annotation/" in n.lower() and n.lower().endswith(".xml")
            ]
            for name in sorted(names):
                try:
                    lons, lats = _parse_s1_annotation_xml(zf.read(name))
                    lons_all.extend(lons)
                    lats_all.extend(lats)
                except Exception as exc:
                    LOG.debug("Skipping %s:%s: %s", product, name, exc)
    else:
        raise ValueError(f"Not a Sentinel-1 SAFE/ZIP product: {product}")

    if len(lons_all) < 4:
        raise RuntimeError(f"Could not read Sentinel-1 geolocation grid: {product}")
    return _robust_lonlat_extent(lons_all, lats_all)


def _find_sentinel_products(path: Path) -> List[Path]:
    if path.is_dir() and path.name.upper().endswith(".SAFE"):
        return [path]
    if path.is_file() and path.suffix.lower() == ".zip" and "S1" in path.name.upper():
        return [path]
    if not path.is_dir():
        return []

    products: List[Path] = []
    products.extend(p for p in path.rglob("*.SAFE") if p.is_dir())
    products.extend(
        p for p in path.rglob("*.zip") if p.is_file() and "S1" in p.name.upper()
    )
    # Avoid duplicate paths while preserving order.
    seen = set()
    out = []
    for p in products:
        key = str(p.resolve())
        if key not in seen:
            out.append(p)
            seen.add(key)
    return out


def _decode_h5_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "item"):
        value = value.item()
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
    return str(value)


def _wkt_lonlat_extent(text: str) -> BBox:
    # NISAR boundingPolygon is a 2-D OGR-compatible WKT geometry. Use
    # shapely when available; otherwise parse coordinate pairs conservatively.
    try:
        from shapely import wkt

        geom = wkt.loads(text)
        # Extract coordinates rather than geom.bounds so antimeridian footprints
        # can still be represented as left > right when appropriate.
        coords: List[Tuple[float, float]] = []

        def collect(g) -> None:
            if hasattr(g, "geoms"):
                for sub in g.geoms:
                    collect(sub)
                return
            if hasattr(g, "exterior") and g.exterior is not None:
                coords.extend((float(x), float(y)) for x, y, *_ in g.exterior.coords)
                for ring in getattr(g, "interiors", []):
                    coords.extend((float(x), float(y)) for x, y, *_ in ring.coords)
                return
            if hasattr(g, "coords"):
                coords.extend((float(x), float(y)) for x, y, *_ in g.coords)

        collect(geom)
        if coords:
            return _robust_lonlat_extent(
                [c[0] for c in coords], [c[1] for c in coords]
            )
    except Exception:
        pass

    pairs = re.findall(
        r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s+"
        r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)",
        text,
    )
    if not pairs:
        raise ValueError("Could not parse NISAR boundingPolygon WKT")
    lons = [float(x) for x, _ in pairs]
    lats = [float(y) for _, y in pairs]
    return _robust_lonlat_extent(lons, lats)


def _nisar_extent(h5_path: Path) -> BBox:
    try:
        import h5py
    except ImportError as exc:
        raise RuntimeError(
            "h5py is required to read NISAR RSLC HDF5 metadata: pip install h5py"
        ) from exc

    with h5py.File(h5_path, "r") as h5:
        base = "/science/LSAR/identification"
        product_key = f"{base}/productType"
        poly_key = f"{base}/boundingPolygon"
        if product_key not in h5 or poly_key not in h5:
            raise RuntimeError(f"Not a recognizable NISAR product: {h5_path}")

        product_type = _decode_h5_text(h5[product_key][()]).upper()
        if "RSLC" not in product_type:
            raise RuntimeError(
                f"Expected NISAR RSLC, got productType={product_type!r}: {h5_path}"
            )

        polygon = _decode_h5_text(h5[poly_key][()])
        return _wkt_lonlat_extent(polygon)


def _find_nisar_products(path: Path) -> List[Path]:
    if path.is_file() and path.suffix.lower() in {".h5", ".hdf5"}:
        return [path]
    if not path.is_dir():
        return []
    return sorted(
        p
        for p in path.rglob("*")
        if p.is_file() and p.suffix.lower() in {".h5", ".hdf5"}
    )


def detect_sar_extent(path: Path) -> Tuple[str, BBox, int]:
    """Return (kind, bbox, product_count), kind is 'sentinel1' or 'nisar'."""
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)

    sentinel = _find_sentinel_products(path)
    nisar = _find_nisar_products(path)

    if sentinel and nisar:
        raise RuntimeError(
            f"Both Sentinel-1 and NISAR products were found under {path}. "
            "Pass a more specific directory or use --bbox."
        )

    if sentinel:
        LOG.info("Found %d Sentinel-1 product(s)", len(sentinel))
        extents = [_sentinel_extent(p) for p in sentinel]
        return "sentinel1", _merge_extents(extents), len(sentinel)

    if nisar:
        LOG.info("Found %d HDF5 file(s); checking for NISAR RSLC metadata", len(nisar))
        extents: List[BBox] = []
        usable = 0
        errors: List[str] = []
        for p in nisar:
            try:
                extents.append(_nisar_extent(p))
                usable += 1
            except Exception as exc:
                errors.append(f"{p.name}: {exc}")
        if extents:
            if errors:
                LOG.debug("Ignored non-RSLC HDF5 files: %s", "; ".join(errors[:5]))
            return "nisar", _merge_extents(extents), usable

    raise RuntimeError(
        f"No Sentinel-1 SAFE/ZIP or NISAR RSLC HDF5 products found under {path}"
    )


def _wrap_lon(lon: float) -> float:
    while lon > 180.0:
        lon -= 360.0
    while lon < -180.0:
        lon += 360.0
    return lon


def buffer_bbox(bbox: BBox, buffer_deg: float) -> BBox:
    """Expand bbox while preserving true antimeridian-crossing semantics."""
    if buffer_deg < 0:
        raise ValueError("buffer must be >= 0")
    left, bottom, right, top = map(float, bbox)
    bottom = max(-90.0, bottom - buffer_deg)
    top = min(90.0, top + buffer_deg)

    if left <= right:
        # A normal AOI near +/-180 must not become a false dateline crossing.
        left = max(-180.0, left - buffer_deg)
        right = min(180.0, right + buffer_deg)
    else:
        # True dateline crossing: retain left > right representation.
        left = _wrap_lon(left - buffer_deg)
        right = _wrap_lon(right + buffer_deg)

    return left, bottom, right, top


# -----------------------------------------------------------------------------
# sardem execution
# -----------------------------------------------------------------------------
def select_dem_source(requested: str, detected_kind: str | None) -> str:
    requested = requested.upper()
    if requested != "AUTO":
        return requested
    if detected_kind == "nisar":
        return "NISAR"
    return "COP"


def _resolve_outputs(fmt: str, output: str | None) -> Tuple[Path | None, Path | None]:
    cwd = Path.cwd()
    if fmt == "isce2":
        p = Path(output or "dem.wgs84")
        if not p.is_absolute():
            p = cwd / p
        return p, None
    if fmt == "isce3":
        p = Path(output or "dem.tif")
        if p.suffix.lower() not in {".tif", ".tiff"}:
            p = Path(str(p) + ".tif")
        if not p.is_absolute():
            p = cwd / p
        return None, p

    base = Path(output or "dem")
    if base.name.lower().endswith(".wgs84"):
        base = base.with_name(base.name[:-6])
    elif base.suffix.lower() in {".tif", ".tiff"}:
        base = base.with_suffix("")
    if not base.is_absolute():
        base = cwd / base
    return Path(str(base) + ".wgs84"), Path(str(base) + ".tif")


def run_sardem(
    bbox: BBox,
    dem_source: str,
    fmt: str,
    output: str | None = None,
    local_vrt: Path | None = None,
) -> Tuple[Path | None, Path | None]:
    """Run sardem with source-specific API compatibility.

    Important
    ---------
    sardem 0.13.0's public ``sardem.dem.main`` does NOT accept
    ``vrt_filename``. Only the Copernicus backend
    ``sardem.cop_dem.download_and_stitch`` accepts it.

    Therefore:
      * NISAR / 3DEP / NASA -> sardem.dem.main()
      * COP with SDEM local VRT -> sardem.cop_dem.download_and_stitch()
      * COP without local VRT -> sardem.dem.main()
    """
    source = dem_source.upper()
    _ensure_sardem_source(source)

    sardem_dem = _get_sardem_dem()
    sardem_utils = _get_sardem_utils()
    isce2_path, isce3_path = _resolve_outputs(fmt, output)

    def run_one(
        output_path: Path,
        output_format: str,
        make_isce_xml: bool,
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if source == "COP" and local_vrt is not None:
            sardem_cop = _get_sardem_cop_dem()
            LOG.info(
                "SDEM backend: sardem.cop_dem.download_and_stitch "
                "(SDEM local COP VRT)"
            )
            sardem_cop.download_and_stitch(
                output_name=str(output_path),
                bbox=bbox,
                keep_egm=False,
                xrate=1,
                yrate=1,
                vrt_filename=str(local_vrt),
                output_format=output_format,
                output_type="float32",
            )
            if make_isce_xml:
                LOG.info("Creating ISCE2 XML file")
                sardem_utils.gdal2isce_xml(
                    str(output_path),
                    keep_egm=False,
                )
            return

        LOG.info("SDEM backend: sardem.dem.main(data_source=%s)", source)
        sardem_dem.main(
            output_name=str(output_path),
            bbox=bbox,
            data_source=source,
            xrate=1,
            yrate=1,
            make_isce_xml=make_isce_xml,
            keep_egm=False,
            output_type="float32",
            output_format=output_format,
        )

    if isce3_path is not None:
        LOG.info(
            "Running sardem %s backend -> ISCE3 GeoTIFF: %s",
            source,
            isce3_path,
        )
        run_one(
            isce3_path,
            output_format="GTiff",
            make_isce_xml=False,
        )

    if isce2_path is not None:
        isce2_path.parent.mkdir(parents=True, exist_ok=True)

        if isce3_path is None:
            LOG.info(
                "Running sardem %s backend -> ISCE2 DEM: %s",
                source,
                isce2_path,
            )
            run_one(
                isce2_path,
                output_format="ENVI",
                make_isce_xml=True,
            )
        else:
            # --format both: scientific DEM generation only once.
            gdal = _get_gdal()
            LOG.info("Translating GeoTIFF -> ISCE2 ENVI: %s", isce2_path)
            ds = gdal.Translate(
                str(isce2_path),
                str(isce3_path),
                format="ENVI",
                outputType=gdal.GDT_Float32,
            )
            if ds is None:
                raise RuntimeError(
                    "gdal.Translate failed while creating ISCE2 DEM"
                )
            ds.FlushCache()
            ds = None
            sardem_utils.gdal2isce_xml(
                str(isce2_path),
                keep_egm=False,
            )

    return isce2_path, isce3_path


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    examples = r"""
Examples:
  # Sentinel-1 SAFE/ZIP -> COP DEM + aria2 -> ISCE2
  python sdem.py ../SLC

  # NISAR RSLC HDF5 -> native NISAR DEM -> ISCE3
  python sdem.py ../RSLC

  # Explicit bbox + NISAR DEM
  python sdem.py \
      --bbox -118.43 33.71 -118.34 33.80 \
      --dem-source nisar \
      --format isce3

  # Force another DEM source
  python sdem.py ../RSLC --dem-source cop
  python sdem.py --bbox -118.43 33.71 -118.34 33.80 --dem-source 3dep

  # Disable SDEM COP aria2 acceleration
  python sdem.py ../SLC --no-aria2

  # COP-only: prefetch/resume tiles and build local VRT
  python sdem.py ../SLC --dem-source cop --vrt-only

Auto mode:
  Sentinel-1 -> DEM source COP   -> output ISCE2
  NISAR RSLC -> DEM source NISAR -> output ISCE3
"""
    p = argparse.ArgumentParser(
        prog="sdem.py",
        description=(
            "SAR-aware wrapper for sardem. Auto-detect Sentinel-1/NISAR "
            "footprints, select an appropriate DEM source, and create "
            "ISCE2/ISCE3 DEM products."
        ),
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "sar_path",
        nargs="?",
        default=None,
        help="SAR file/directory: Sentinel-1 SAFE/ZIP or NISAR RSLC HDF5",
    )
    p.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("LEFT", "BOTTOM", "RIGHT", "TOP"),
        help="Explicit bbox [left bottom right top]; overrides SAR metadata",
    )
    p.add_argument(
        "--buffer-deg",
        "--buffer",
        type=float,
        default=0.2,
        help="Expand SAR bbox on all sides before DEM generation (default: 0.2 deg)",
    )
    p.add_argument(
        "--dem-source",
        choices=["auto", "cop", "nisar", "3dep", "nasa"],
        default="auto",
        help="sardem DEM source. auto: Sentinel-1->COP, NISAR->NISAR",
    )
    p.add_argument(
        "--format",
        choices=["auto", "isce2", "isce3", "both"],
        default="auto",
        help="Output mode. auto: Sentinel-1->isce2, NISAR->isce3",
    )
    p.add_argument(
        "-o",
        "--output",
        help=(
            "Output path. Defaults: dem.wgs84 (isce2), dem.tif (isce3), "
            "basename dem for both"
        ),
    )
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="SDEM Copernicus aria2 tile cache (default: ~/.cache/sdem/cop30)",
    )
    p.add_argument(
        "--aria2-jobs",
        type=int,
        default=8,
        help="Concurrent COP tile downloads (default: 8)",
    )
    p.add_argument(
        "--aria2-connections",
        type=int,
        default=4,
        help="Connections per COP tile (default: 4)",
    )
    p.add_argument(
        "--refresh-tile-list",
        action="store_true",
        help="Refresh cached Copernicus tile list",
    )
    p.add_argument(
        "--no-aria2",
        action="store_true",
        help=(
            "For COP source, bypass SDEM's local tile cache and use "
            "sardem's native remote VRT"
        ),
    )
    p.add_argument(
        "--vrt-only",
        action="store_true",
        help="COP+aria2 only: prefetch tiles/build local VRT and exit",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"SDEM {SDEM_VERSION}",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show verbose/debug logging",
    )
    return p

def main(argv: Sequence[str] | None = None) -> int:
    print_banner()
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    detected_kind: str | None = None

    if args.bbox is not None:
        raw_bbox = tuple(map(float, args.bbox))
        LOG.info("Using explicit bbox: %s", raw_bbox)
    else:
        if not args.sar_path:
            raise SystemExit(
                "Provide SAR_PATH or --bbox LEFT BOTTOM RIGHT TOP"
            )
        detected_kind, raw_bbox, count = detect_sar_extent(
            Path(args.sar_path)
        )
        LOG.info(
            "Detected %s extent from %d product(s): %s",
            detected_kind,
            count,
            raw_bbox,
        )

    bbox = buffer_bbox(raw_bbox, args.buffer_deg)
    LOG.info(
        "DEM bbox after %.3f deg buffer: %s",
        args.buffer_deg,
        bbox,
    )

    dem_source = select_dem_source(args.dem_source, detected_kind)
    LOG.info("DEM source -> %s", dem_source)

    fmt = args.format
    if fmt == "auto":
        fmt = "isce2" if detected_kind == "sentinel1" else "isce3"
        LOG.info("Auto output format -> %s", fmt)

    local_vrt: Path | None = None

    if dem_source == "COP" and not args.no_aria2:
        local_vrt = prepare_local_vrt(
            bbox=bbox,
            cache_dir=args.cache_dir,
            jobs=max(1, args.aria2_jobs),
            connections_per_file=max(1, args.aria2_connections),
            refresh_tile_list=args.refresh_tile_list,
        )
        LOG.info("Local Copernicus VRT: %s", local_vrt)

    elif dem_source == "COP":
        LOG.info(
            "COP source: aria2 acceleration disabled; "
            "using sardem native remote VRT"
        )

    else:
        LOG.info(
            "%s source uses sardem native backend; "
            "aria2 options are not used",
            dem_source,
        )

    if args.vrt_only:
        if dem_source != "COP" or args.no_aria2:
            raise RuntimeError(
                "--vrt-only requires --dem-source cop with aria2 enabled"
            )
        print(local_vrt)
        return 0

    isce2_path, isce3_path = run_sardem(
        bbox=bbox,
        dem_source=dem_source,
        fmt=fmt,
        output=args.output,
        local_vrt=local_vrt,
    )

    if isce2_path is not None:
        LOG.info("ISCE2 DEM: %s", isce2_path)
        LOG.info("ISCE2 XML: %s.xml", isce2_path)

    if isce3_path is not None:
        LOG.info("ISCE3 DEM: %s", isce3_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())