#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sdem.py - SAR-aware DEM wrapper around sardem.

SDEM adds SAR-product awareness on top of sardem:

1. Detect a geographic extent from Sentinel-1 SAFE/ZIP or NISAR RSLC HDF5.
2. Select a suitable sardem DEM source automatically.
3. For Copernicus DEMs, optionally accelerate downloads with aria2 and a
   persistent local tile/VRT cache.
4. Delegate DEM grid generation, reprojection and vertical-datum handling to
   sardem.

Auto mode
---------
    Sentinel-1 -> sardem COP source -> ISCE2 DEM
    NISAR RSLC -> sardem NISAR source -> ISCE3 GeoTIFF

Author: Shuai Wang
Affiliation: China University of Mining and Technology
"""
from __future__ import annotations

import argparse
import hashlib
import json
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

BBox = Tuple[float, float, float, float]
SDEM_VERSION = "1.1.0"

BANNER = r"""
   ███████╗██████╗ ███████╗███╗   ███╗
   ██╔════╝██╔══██╗██╔════╝████╗ ████║
   ███████╗██║  ██║█████╗  ██╔████╔██║
   ╚════██║██║  ██║██╔══╝  ██║╚██╔╝██║
   ███████║██████╔╝███████╗██║ ╚═╝ ██║
   ╚══════╝╚═════╝ ╚══════╝╚═╝     ╚═╝

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
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
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


def _ensure_sardem_source(source: str) -> None:
    try:
        from sardem.download import Downloader
    except ImportError as exc:
        raise RuntimeError("sardem is required: python -m pip install -U sardem") from exc
    valid = {str(x).upper() for x in Downloader.VALID_SOURCES}
    if source.upper() not in valid:
        raise RuntimeError(
            f"Installed sardem does not support data source {source!r}. "
            "Upgrade sardem (NISAR support requires a recent sardem release)."
        )


# -----------------------------------------------------------------------------
# Copernicus + aria2 acceleration
# -----------------------------------------------------------------------------

def _default_cache_dir() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "sdem" / "cop30"


def _tile_name(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def _split_dateline(bbox: BBox) -> List[BBox]:
    utils = _get_sardem_utils()
    return [tuple(map(float, b)) for b in utils.check_dateline(bbox)]


def _aligned_bbox(bbox: BBox) -> BBox:
    utils = _get_sardem_utils()
    return tuple(map(float, utils.align_bounds_to_pixel_grid(bbox)))


def _candidate_tile_origins(bbox: BBox) -> Iterable[Tuple[int, int]]:
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


def _read_tile_list(cache_dir: Path, refresh: bool = False, timeout: int = 60) -> set[str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    tile_list_file = cache_dir / "tileList.txt"
    if refresh or not tile_list_file.exists() or tile_list_file.stat().st_size == 0:
        LOG.info("Fetching Copernicus tile list")
        r = requests.get(COP30_TILE_LIST_URL, timeout=timeout)
        r.raise_for_status()
        tile_list_file.write_bytes(r.content)

    names = {
        line.strip().strip("\r")
        for line in tile_list_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip()
    }
    if not names:
        raise RuntimeError(f"Copernicus tile list is empty: {tile_list_file}")
    return names


def required_tile_names(bbox: BBox, cache_dir: Path, refresh_tile_list: bool = False) -> List[str]:
    available = _read_tile_list(cache_dir, refresh=refresh_tile_list)
    names: set[str] = set()
    for sub_bbox in _split_dateline(bbox):
        for lat, lon in _candidate_tile_origins(sub_bbox):
            name = _tile_name(lat, lon)
            if name in available:
                names.add(name)
            else:
                LOG.debug("No public COP30 tile: %s", name)
    return sorted(names)


def _tile_path(cache_dir: Path, name: str) -> Path:
    return cache_dir / "tiles" / f"{name}.tif"


def _tile_url(name: str) -> str:
    return f"{COP30_BASE_URL}/{name}/{name}.tif"


def _aria2_control_path(path: Path) -> Path:
    return Path(str(path) + ".aria2")


def _validate_raster(path: Path, deep: bool = True) -> bool:
    """Validate cached TIFF and, when requested, force compressed blocks to decode."""
    if not path.exists() or path.stat().st_size <= 0:
        return False
    if _aria2_control_path(path).exists():
        # aria2 sidecar means this is intentionally incomplete/resumable.
        return False
    try:
        gdal = _get_gdal()
        ds = gdal.Open(str(path), gdal.GA_ReadOnly)
        if ds is None or ds.RasterXSize <= 0 or ds.RasterYSize <= 0 or ds.RasterCount <= 0:
            ds = None
            return False
        if deep:
            for band_idx in range(1, ds.RasterCount + 1):
                band = ds.GetRasterBand(band_idx)
                if band is None:
                    ds = None
                    return False
                band.Checksum()  # Forces raster blocks to be decoded/read.
        ds = None
        return True
    except Exception as exc:
        LOG.debug("Raster validation failed for %s: %s", path, exc)
        return False


def prefetch_tiles(
    names: Sequence[str],
    cache_dir: Path | None = None,
    jobs: int = 8,
    connections_per_file: int = 4,
    retries: int = 5,
    timeout: int = 60,
) -> List[Path]:
    cache_dir = Path(cache_dir or _default_cache_dir()).expanduser().resolve()
    tiles_dir = cache_dir / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)

    aria2 = shutil.which("aria2c")
    if not aria2:
        raise RuntimeError("aria2c not found. Install it, e.g. sudo apt install aria2")

    valid: List[Path] = []
    pending: List[Tuple[str, Path]] = []
    resume_count = 0

    for name in names:
        path = _tile_path(cache_dir, name)
        control = _aria2_control_path(path)

        if control.exists():
            # Preserve partial file and sidecar; aria2 will resume it.
            resume_count += 1
            pending.append((name, path))
            LOG.info("aria2: resuming incomplete tile: %s", path.name)
            continue

        if _validate_raster(path, deep=True):
            valid.append(path)
            continue

        if path.exists():
            LOG.warning("Removing invalid cached tile: %s", path)
            path.unlink(missing_ok=True)
        control.unlink(missing_ok=True)
        pending.append((name, path))

    if pending:
        LOG.info(
            "aria2: downloading/resuming %d COP30 tiles (%d resumable, %d cached)",
            len(pending),
            resume_count,
            len(valid),
        )
        with tempfile.NamedTemporaryFile("w", suffix=".aria2.txt", delete=False) as f:
            input_file = Path(f.name)
            for name, path in pending:
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
    incomplete: List[str] = []
    for name in names:
        path = _tile_path(cache_dir, name)
        if _aria2_control_path(path).exists():
            incomplete.append(name)
        elif _validate_raster(path, deep=True):
            paths.append(path)
        else:
            bad.append(name)

    if incomplete:
        raise RuntimeError(
            "COP30 download is still incomplete (aria2 control file remains): "
            + ", ".join(incomplete[:10])
        )
    if bad:
        for name in bad:
            _tile_path(cache_dir, name).unlink(missing_ok=True)
        raise RuntimeError(
            "Downloaded COP30 tiles failed full raster validation and were removed: "
            + ", ".join(bad[:10])
        )
    return paths


def build_local_vrt(tile_paths: Sequence[Path], vrt_path: Path) -> Path:
    if not tile_paths:
        raise RuntimeError("No Copernicus DEM tiles available for this bbox")
    gdal = _get_gdal()
    vrt_path.parent.mkdir(parents=True, exist_ok=True)
    options = gdal.BuildVRTOptions(
        resolution="highest",
        resampleAlg="nearest",
        outputSRS="EPSG:4326+3855",
    )
    ds = gdal.BuildVRT(str(vrt_path), [str(p) for p in tile_paths], options=options)
    if ds is None:
        raise RuntimeError("gdal.BuildVRT failed")
    ds.FlushCache()
    ds = None
    return vrt_path


def _vrt_cache_key(bbox: BBox, tile_names: Sequence[str]) -> str:
    payload = repr((tuple(round(v, 12) for v in bbox), tuple(tile_names))).encode()
    return hashlib.sha1(payload).hexdigest()[:12]


def prepare_local_vrt(
    bbox: BBox,
    cache_dir: Path | None = None,
    jobs: int = 8,
    connections_per_file: int = 4,
    refresh_tile_list: bool = False,
) -> Path:
    cache_dir = Path(cache_dir or _default_cache_dir()).expanduser().resolve()
    names = required_tile_names(bbox, cache_dir, refresh_tile_list=refresh_tile_list)
    LOG.info("COP30 tiles intersecting bbox: %d", len(names))
    tile_paths = prefetch_tiles(
        names,
        cache_dir=cache_dir,
        jobs=jobs,
        connections_per_file=connections_per_file,
    )
    key = _vrt_cache_key(bbox, names)
    vrt_path = cache_dir / "vrts" / f"cop30_{key}.vrt"
    # Always rebuild; it is cheap and ensures it only references validated tiles.
    build_local_vrt(tile_paths, vrt_path)
    return vrt_path


# -----------------------------------------------------------------------------
# SAR extent detection
# -----------------------------------------------------------------------------

def _parse_s1_annotation_xml(xml_bytes: bytes) -> Tuple[List[float], List[float]]:
    root = ET.fromstring(xml_bytes)
    lons: List[float] = []
    lats: List[float] = []
    for point in root.iter():
        if point.tag.split("}")[-1] != "geolocationGridPoint":
            continue
        lon = lat = None
        for child in point:
            tag = child.tag.split("}")[-1]
            if tag == "longitude":
                lon = float(child.text)
            elif tag == "latitude":
                lat = float(child.text)
        if lon is not None and lat is not None:
            lons.append(lon)
            lats.append(lat)
    return lons, lats


def _extent_from_safe_dir(path: Path) -> BBox:
    lons: List[float] = []
    lats: List[float] = []
    for xml in sorted((path / "annotation").glob("*.xml")):
        try:
            lo, la = _parse_s1_annotation_xml(xml.read_bytes())
            lons.extend(lo)
            lats.extend(la)
        except Exception:
            continue
    if not lons:
        raise RuntimeError(f"No Sentinel-1 geolocation grid found in {path}")
    return _robust_extent(lons, lats)


def _extent_from_s1_zip(path: Path) -> BBox:
    lons: List[float] = []
    lats: List[float] = []
    with zipfile.ZipFile(path, "r") as zf:
        for name in zf.namelist():
            if "/annotation/" not in name or not name.lower().endswith(".xml"):
                continue
            try:
                lo, la = _parse_s1_annotation_xml(zf.read(name))
                lons.extend(lo)
                lats.extend(la)
            except Exception:
                continue
    if not lons:
        raise RuntimeError(f"No Sentinel-1 geolocation grid found in {path}")
    return _robust_extent(lons, lats)


def _decode_h5_scalar(value) -> str:
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def _h5_scalar(group, name: str) -> str | None:
    obj = group.get(name) if group is not None else None
    if obj is None:
        return None
    try:
        return _decode_h5_scalar(obj[()]).strip()
    except Exception:
        return None


def _nisar_rslc_identity(f, path: Path) -> tuple[bool, str]:
    ident = f.get("/science/LSAR/identification")
    rslc = f.get("/science/LSAR/RSLC")
    product_type = (_h5_scalar(ident, "productType") or "").upper()
    mission = (_h5_scalar(ident, "missionId") or "").upper()
    name = path.name.upper()

    signals = []
    if "RSLC" in product_type:
        signals.append(f"productType={product_type}")
    if rslc is not None:
        signals.append("/science/LSAR/RSLC present")
    if mission == "NISAR":
        signals.append("missionId=NISAR")
    if name.startswith("NISAR_") and "_RSLC_" in name:
        signals.append("RSLC granule filename")

    recognized = rslc is not None and (
        "RSLC" in product_type
        or mission == "NISAR"
        or (name.startswith("NISAR_") and "_RSLC_" in name)
        or ident is None
    )
    if not recognized and "RSLC" in product_type:
        recognized = True
    return recognized, ", ".join(signals) or "no NISAR RSLC markers"


def _geometry_lonlat_pairs(obj) -> tuple[List[float], List[float]]:
    lons: List[float] = []
    lats: List[float] = []

    def walk(node):
        if isinstance(node, (list, tuple)):
            if len(node) >= 2 and all(isinstance(v, (int, float)) for v in node[:2]):
                lons.append(float(node[0]))
                lats.append(float(node[1]))
            else:
                for child in node:
                    walk(child)

    if isinstance(obj, dict):
        walk(obj.get("coordinates", []))
    return lons, lats


def _extent_from_polygon_text(text: str) -> BBox:
    text = text.strip()
    if not text:
        raise ValueError("empty boundingPolygon")

    if text.startswith("{"):
        try:
            obj = json.loads(text)
            lons, lats = _geometry_lonlat_pairs(obj)
            if lons and lats:
                return _robust_extent(lons, lats)
        except Exception:
            pass

    try:
        from shapely import wkt

        geom = wkt.loads(text)
        if not geom.is_empty:
            minx, miny, maxx, maxy = geom.bounds
            return float(minx), float(miny), float(maxx), float(maxy)
    except Exception:
        pass

    number = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
    pairs = re.findall(rf"({number})\s+({number})", text)
    if pairs:
        lons = [float(x) for x, _ in pairs]
        lats = [float(y) for _, y in pairs]
        if all(-90.0001 <= y <= 90.0001 for y in lats):
            return _robust_extent(lons, lats)

    raise ValueError("boundingPolygon is neither usable WKT nor GeoJSON")


def _finite_h5_values(dataset) -> List[float]:
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("numpy is required for NISAR geolocation-grid fallback") from exc

    arr = np.asarray(dataset[...], dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return []
    return arr.ravel().tolist()


def _extent_from_nisar_geolocation_grid(f) -> BBox:
    grid = f.get("/science/LSAR/RSLC/metadata/geolocationGrid")
    if grid is None:
        raise KeyError("/science/LSAR/RSLC/metadata/geolocationGrid is missing")
    x = grid.get("coordinateX")
    y = grid.get("coordinateY")
    if x is None or y is None:
        raise KeyError("geolocationGrid coordinateX/coordinateY is missing")

    lons_all = _finite_h5_values(x)
    lats_all = _finite_h5_values(y)
    if not lons_all or not lats_all:
        raise ValueError("geolocationGrid coordinateX/coordinateY contains no finite values")

    # This fallback is only safe when the grid stores geographic coordinates.
    lons = [v for v in lons_all if -360.1 <= v <= 360.1]
    lats = [v for v in lats_all if -90.1 <= v <= 90.1]
    if not lons or not lats:
        raise ValueError(
            "geolocationGrid coordinateX/coordinateY do not contain usable longitude/latitude degrees"
        )
    return _robust_extent(lons, lats)


def _extent_from_nisar_h5(path: Path) -> BBox:
    try:
        import h5py
    except ImportError as exc:
        raise RuntimeError(
            "h5py is required for NISAR RSLC detection. Install it with: "
            "python -m pip install h5py"
        ) from exc

    try:
        f = h5py.File(path, "r")
    except Exception as exc:
        raise RuntimeError(f"Cannot open HDF5 file: {exc}") from exc

    with f:
        recognized, identity = _nisar_rslc_identity(f, path)
        if not recognized:
            raise RuntimeError(f"Not recognized as NISAR RSLC ({identity})")

        ident = f.get("/science/LSAR/identification")
        polygon_error = None
        polygon_text = _h5_scalar(ident, "boundingPolygon")
        if polygon_text:
            try:
                bbox = _extent_from_polygon_text(polygon_text)
                LOG.debug("NISAR footprint from boundingPolygon: %s", path.name)
                return bbox
            except Exception as exc:
                polygon_error = exc

        try:
            bbox = _extent_from_nisar_geolocation_grid(f)
            LOG.debug("NISAR footprint from RSLC geolocationGrid: %s", path.name)
            return bbox
        except Exception as grid_exc:
            details = []
            if not polygon_text:
                details.append("identification/boundingPolygon missing")
            elif polygon_error is not None:
                details.append(f"boundingPolygon unusable: {polygon_error}")
            details.append(f"geolocationGrid fallback failed: {grid_exc}")
            raise RuntimeError("; ".join(details)) from grid_exc


def _robust_extent(lons: Sequence[float], lats: Sequence[float]) -> BBox:
    if not lons or not lats:
        raise ValueError("No coordinates")
    min_lat = min(lats)
    max_lat = max(lats)
    raw_span = max(lons) - min(lons)
    shifted = [lon + 360.0 if lon < 0 else lon for lon in lons]
    shifted_span = max(shifted) - min(shifted)
    if shifted_span < raw_span and shifted_span < 180.0:
        left_s = min(shifted)
        right_s = max(shifted)
        left = left_s if left_s <= 180 else left_s - 360
        right = right_s if right_s <= 180 else right_s - 360
        return float(left), float(min_lat), float(right), float(max_lat)
    return float(min(lons)), float(min_lat), float(max(lons)), float(max_lat)


def _merge_extents(extents: Sequence[BBox]) -> BBox:
    if not extents:
        raise ValueError("No extents")
    lats_bottom = [e[1] for e in extents]
    lats_top = [e[3] for e in extents]
    points: List[float] = []
    for left, _, right, _ in extents:
        if left <= right:
            points.extend([left, right])
        else:
            points.extend([left, right, 180.0, -180.0])
    left, _, right, _ = _robust_extent(points, [min(lats_bottom), max(lats_top)])
    return left, min(lats_bottom), right, max(lats_top)


def _format_failures(failures: Sequence[tuple[Path, Exception]], limit: int = 5) -> str:
    if not failures:
        return ""
    lines = []
    for path, exc in failures[:limit]:
        lines.append(f"  - {path.name}: {type(exc).__name__}: {exc}")
    if len(failures) > limit:
        lines.append(f"  ... and {len(failures) - limit} more")
    return "\n".join(lines)


def detect_sar_extent(path: Path) -> Tuple[str, BBox, int]:
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)

    if path.is_file():
        if path.suffix.lower() == ".h5":
            return "nisar", _extent_from_nisar_h5(path), 1
        if path.suffix.lower() == ".zip":
            return "sentinel1", _extent_from_s1_zip(path), 1
        raise ValueError(f"Unsupported SAR file: {path}")

    safes = sorted(p for p in path.rglob("*.SAFE") if p.is_dir())
    zips = sorted(p for p in path.rglob("*.zip") if p.is_file() and p.name.upper().startswith("S1"))
    h5s = sorted(p for p in path.rglob("*.h5") if p.is_file())

    if safes or zips:
        extents: List[BBox] = []
        failures: List[tuple[Path, Exception]] = []
        for p in safes:
            try:
                extents.append(_extent_from_safe_dir(p))
            except Exception as exc:
                failures.append((p, exc))
                LOG.debug("Skipping %s: %s", p, exc)
        for p in zips:
            try:
                extents.append(_extent_from_s1_zip(p))
            except Exception as exc:
                failures.append((p, exc))
                LOG.debug("Skipping %s: %s", p, exc)
        if not extents:
            msg = "Sentinel-1 products found but none yielded a valid footprint"
            details = _format_failures(failures)
            if details:
                msg += "\n" + details
            raise RuntimeError(msg)
        LOG.info("Found %d usable Sentinel-1 product(s)", len(extents))
        return "sentinel1", _merge_extents(extents), len(extents)

    if h5s:
        extents: List[BBox] = []
        failures: List[tuple[Path, Exception]] = []
        for p in h5s:
            try:
                extents.append(_extent_from_nisar_h5(p))
            except Exception as exc:
                failures.append((p, exc))
                LOG.debug("Skipping %s: %s", p, exc)
        if not extents:
            msg = (
                f"Found {len(h5s)} HDF5 file(s), but no usable NISAR RSLC footprint was extracted.\n"
                "Reasons from the first file(s):"
            )
            details = _format_failures(failures)
            if details:
                msg += "\n" + details
            msg += (
                "\nRun again with -v for debug logging. If these are subset/repacked RSLC files, "
                "they must retain either identification/boundingPolygon or a usable "
                "RSLC geolocation grid."
            )
            raise RuntimeError(msg)
        if failures:
            LOG.warning("Skipped %d non-usable HDF5 file(s); use -v for details", len(failures))
        LOG.info("Found %d usable NISAR RSLC product(s)", len(extents))
        return "nisar", _merge_extents(extents), len(extents)

    raise RuntimeError(f"No Sentinel-1 SAFE/ZIP or NISAR RSLC HDF5 found under {path}")


# -----------------------------------------------------------------------------
# Backend selection / output
# -----------------------------------------------------------------------------

def _wrap_lon(lon: float) -> float:
    while lon > 180.0:
        lon -= 360.0
    while lon < -180.0:
        lon += 360.0
    return lon


def buffer_bbox(bbox: BBox, buffer_deg: float) -> BBox:
    if buffer_deg < 0:
        raise ValueError("buffer must be >= 0")
    left, bottom, right, top = map(float, bbox)
    bottom = max(-90.0, bottom - buffer_deg)
    top = min(90.0, top + buffer_deg)
    if left <= right:
        left = max(-180.0, left - buffer_deg)
        right = min(180.0, right + buffer_deg)
    else:
        left = _wrap_lon(left - buffer_deg)
        right = _wrap_lon(right + buffer_deg)
    return left, bottom, right, top


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
    """Run sardem using the selected native data source."""
    source = dem_source.upper()
    _ensure_sardem_source(source)
    sardem_dem = _get_sardem_dem()
    sardem_utils = _get_sardem_utils()
    isce2_path, isce3_path = _resolve_outputs(fmt, output)

    vrt_filename = str(local_vrt) if local_vrt is not None else None
    LOG.info("SDEM backend: sardem.dem.main(data_source=%s)", source)

    common = dict(
        bbox=bbox,
        data_source=source,
        xrate=1,
        yrate=1,
        keep_egm=False,
        output_type="float32",
        vrt_filename=vrt_filename,
    )

    if isce3_path is not None:
        isce3_path.parent.mkdir(parents=True, exist_ok=True)
        LOG.info("Running sardem %s backend -> ISCE3 GeoTIFF: %s", source, isce3_path)
        sardem_dem.main(
            output_name=str(isce3_path),
            output_format="GTiff",
            make_isce_xml=False,
            **common,
        )

    if isce2_path is not None:
        isce2_path.parent.mkdir(parents=True, exist_ok=True)
        if isce3_path is None:
            LOG.info("Running sardem %s backend -> ISCE2 DEM: %s", source, isce2_path)
            sardem_dem.main(
                output_name=str(isce2_path),
                output_format="ENVI",
                make_isce_xml=True,
                **common,
            )
        else:
            # Avoid downloading/warping twice in --format both mode.
            gdal = _get_gdal()
            LOG.info("Translating GeoTIFF -> ISCE2 ENVI: %s", isce2_path)
            ds = gdal.Translate(
                str(isce2_path),
                str(isce3_path),
                format="ENVI",
                outputType=gdal.GDT_Float32,
            )
            if ds is None:
                raise RuntimeError("gdal.Translate failed while creating ISCE2 DEM")
            ds.FlushCache()
            ds = None
            sardem_utils.gdal2isce_xml(str(isce2_path), keep_egm=False)

    return isce2_path, isce3_path


def build_parser() -> argparse.ArgumentParser:
    examples = r"""
Examples:
  # Sentinel-1 SAFE/ZIP -> COP DEM + aria2 -> ISCE2
  python sdem.py ../SLC

  # NISAR RSLC HDF5 -> native NISAR DEM -> ISCE3
  python sdem.py ../RSLC

  # Force a DEM source
  python sdem.py ../RSLC --dem-source cop
  python sdem.py ../SLC --dem-source nisar --format isce3
  python sdem.py --bbox -118.43 33.71 -118.34 33.80 --dem-source 3dep

  # Disable SDEM's aria2 acceleration and let sardem use its native COP VRT
  python sdem.py ../SLC --no-aria2

  # Resume/parallelize Copernicus tile downloads
  python sdem.py ../SLC --aria2-jobs 8 --aria2-connections 4

  # COP-only: prefetch tiles and build local VRT without creating DEM
  python sdem.py ../SLC --dem-source cop --vrt-only

Auto mode:
  Sentinel-1 -> DEM source COP   -> output ISCE2
  NISAR RSLC -> DEM source NISAR -> output ISCE3

SDEM extracts the SAR extent and selects the backend. Final DEM processing is
performed by sardem. aria2 acceleration is used only for the COP source.
"""
    p = argparse.ArgumentParser(
        prog="sdem.py",
        description=(
            "SAR-aware wrapper for sardem. Auto-detect Sentinel-1/NISAR footprints, "
            "select an appropriate DEM source, and create ISCE2/ISCE3 DEM products."
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
        help="Output path. Defaults: dem.wgs84 (isce2), dem.tif (isce3), basename dem for both",
    )
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="SDEM Copernicus aria2 tile cache (default: ~/.cache/sdem/cop30)",
    )
    p.add_argument("--aria2-jobs", type=int, default=8, help="Concurrent COP tile downloads")
    p.add_argument("--aria2-connections", type=int, default=4, help="Connections per COP tile")
    p.add_argument("--refresh-tile-list", action="store_true", help="Refresh cached Copernicus tile list")
    p.add_argument(
        "--no-aria2",
        action="store_true",
        help="For COP source, bypass SDEM's aria2 cache and use sardem's native remote VRT",
    )
    p.add_argument(
        "--vrt-only",
        action="store_true",
        help="COP+aria2 only: prefetch tiles/build local VRT and exit",
    )
    p.add_argument("--version", action="version", version=f"SDEM {SDEM_VERSION}")
    p.add_argument("-v", "--verbose", action="store_true", help="Show verbose/debug logging")
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
            raise SystemExit("Provide SAR_PATH or --bbox LEFT BOTTOM RIGHT TOP")
        detected_kind, raw_bbox, count = detect_sar_extent(Path(args.sar_path))
        LOG.info("Detected %s extent from %d product(s): %s", detected_kind, count, raw_bbox)

    bbox = buffer_bbox(raw_bbox, args.buffer_deg)
    LOG.info("DEM bbox after %.3f deg buffer: %s", args.buffer_deg, bbox)

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
        LOG.info("COP source: aria2 acceleration disabled; using sardem native remote VRT")
    else:
        LOG.info("%s source uses sardem native backend; aria2 options are not used", dem_source)

    if args.vrt_only:
        if dem_source != "COP" or args.no_aria2:
            raise RuntimeError("--vrt-only requires --dem-source cop with aria2 enabled")
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