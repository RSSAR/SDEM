# SDEM examples

## 1. Sentinel-1 stack -> ISCE2 DEM

Assume:

```text
project/
├── SLC/
│   ├── S1A_*.SAFE/
│   └── ...
└── DEM/
    └── sdem.py
```

Run:

```bash
cd DEM
python sdem.py ../SLC
```

Expected auto mode:

```text
Sentinel-1 -> isce2
```

Typical products:

```text
dem.wgs84
dem.hdr
dem.wgs84.xml
```

## 2. NISAR RSLC -> ISCE3 DEM

```bash
python sdem.py ../RSLC
```

Expected auto mode:

```text
NISAR RSLC -> isce3
```

Output:

```text
dem.tif
```

## 3. Explicit bbox

```bash
python sdem.py --bbox -118.43 33.71 -118.34 33.80 --format isce3 -o dem.tif
```

## 4. Change the buffer

```bash
python sdem.py ../SLC --buffer 0.1
```

## 5. Increase aria2 parallelism

```bash
python sdem.py ../SLC --aria2-jobs 16 --aria2-connections 4
```

## 6. Build the local Copernicus VRT only

```bash
python sdem.py ../SLC --vrt-only
```

## 7. Inspect the generated ISCE2 DEM

```bash
gdalinfo dem.wgs84 | head -40
grep -A4 -B1 -i "reference" dem.wgs84.xml
gdalinfo -stats dem.wgs84 | grep -E "Minimum|Maximum|Mean|NoData"
```

For a correctly prepared processor-ready ISCE2 DEM, the XML reference should report `WGS84`.
