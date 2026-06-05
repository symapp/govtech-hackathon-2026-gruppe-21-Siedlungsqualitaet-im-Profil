#!/usr/bin/env python3
"""
Sample settlement-quality layers at representative Swiss places and write
docs/PRESET_STATISTICS.md plus data-pipelines/output/city_factor_profiles.json.

Run from repo root:
  uv run python data-pipelines/analyze_settlement_profiles.py
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import numpy as np

try:
    import pyproj
    import xarray as xr
except ImportError as exc:  # pragma: no cover
    print("Requires pyproj and xarray:", exc, file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = Path(__file__).resolve().parent / "output" / "city_factor_profiles.json"
OUT_MD = REPO_ROOT / "docs" / "PRESET_STATISTICS.md"

_bucket = (
    os.getenv("B2_BUCKET_NAME")
    or os.getenv("S3_BUCKET_NAME")
    or os.getenv("MINIO_BUCKET_NAME")
    or "egov-hackathon"
)
_endpoint = (
    os.getenv("B2_ENDPOINT_URL")
    or os.getenv("S3_ENDPOINT_URL")
    or os.getenv("MINIO_ENDPOINT_URL")
    or "http://127.0.0.1:9000"
).rstrip("/")
ZARR_BASE = os.getenv("ZARR_BASE_URL", f"{_endpoint}/{_bucket}")
META_NAME = "settlement-layer-meta.json"

WGS84_TO_LV95 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True)

# Featured places + extra urban / suburban anchors
CITIES: list[dict[str, Any]] = [
    {"id": "zurich", "name": "Zürich", "archetype": "urban", "lat": 47.3769, "lng": 8.5417},
    {"id": "geneve", "name": "Genève", "archetype": "urban", "lat": 46.2044, "lng": 6.1432},
    {"id": "basel", "name": "Basel", "archetype": "urban", "lat": 47.5596, "lng": 7.5886},
    {"id": "bern", "name": "Bern", "archetype": "urban", "lat": 46.948, "lng": 7.4474},
    {"id": "lausanne", "name": "Lausanne", "archetype": "urban", "lat": 46.5197, "lng": 6.6323},
    {"id": "lugano", "name": "Lugano", "archetype": "urban", "lat": 46.0037, "lng": 8.9511},
    {"id": "st-gallen", "name": "St. Gallen", "archetype": "mid", "lat": 47.4245, "lng": 9.3767},
    {"id": "thun", "name": "Thun", "archetype": "suburban", "lat": 46.7579, "lng": 7.6282},
    {"id": "murten", "name": "Murten", "archetype": "small", "lat": 46.9283, "lng": 7.1171},
    {"id": "scuol", "name": "Scuol", "archetype": "alpine", "lat": 46.7976, "lng": 10.2992},
    {"id": "glarus", "name": "Glarus", "archetype": "small", "lat": 47.0406, "lng": 9.068},
    {"id": "adelboden", "name": "Adelboden", "archetype": "alpine", "lat": 46.4919, "lng": 7.5606},
    {"id": "appenzell", "name": "Appenzell", "archetype": "rural", "lat": 47.331, "lng": 9.409},
]

LAYERS: list[dict[str, Any]] = [
    {"id": "tranquillity", "store": "ch_bafu_tranquillity_karte.zarr", "var": "tranquillity_index", "hib": True, "clim": [0, 1]},
    {"id": "population-density", "store": "statpop_population_density_100m.zarr", "var": "population_density_score", "hib": False, "clim": [300, 9900]},
    {"id": "pt-accessibility", "store": "erreichbarkeit_swiss_grid_100m.zarr", "var": "OeV_Erreichb_EW", "hib": True, "clim": [0, 1]},
    {"id": "miv-accessibility", "store": "erreichbarkeit_miv_swiss_grid_100m.zarr", "var": "Strasse_Erreichb_EW", "hib": True, "clim": [50, 3500]},
    {"id": "pt-quality", "store": "pt_quality_swiss_grid_100m.zarr", "var": "KLASSE_NUM", "hib": True, "clim": [1, 4]},
    {"id": "pt-travel-time", "store": "reisezeit_oev_swiss_grid_100m.zarr", "var": "OeV_Reisezeit_Z", "hib": False, "clim": [15, 90]},
    {"id": "miv-travel-time", "store": "reisezeit_miv_swiss_grid_100m.zarr", "var": "Strasse_Reisezeit_Z", "hib": False, "clim": [10, 75]},
    {"id": "rail-traffic", "store": "belastung_bahn_swiss_grid_100m.zarr", "var": "DTV_OEV", "hib": False, "clim": [500, 25000]},
    {"id": "road-traffic", "store": "belastung_strasse_swiss_grid_100m.zarr", "var": "DTV_FZG", "hib": False, "clim": [500, 20000]},
    {"id": "secondary-homes", "store": "zweitwohnungsanteil_swiss_grid_100m.zarr", "var": "ZWG_3110", "hib": False, "clim": [0, 40]},
    {"id": "landscape-type", "store": "landschaftstypen_swiss_grid_100m.zarr", "var": "TYP_NR", "hib": True, "clim": [1, 40]},
    {"id": "solar-potential", "store": "solar_nutzungsaspekte.zarr", "var": "solar_suitability", "hib": True, "clim": [1, 5]},
    {"id": "tlm-green-trees", "store": "tlm_green_trees_swiss_grid_100m.zarr", "var": "green_amenity_index", "hib": True, "clim": [0, 0.57]},
]

ARCHETYPE_GROUPS = {
    "urban": ["zurich", "geneve", "basel", "bern", "lausanne", "lugano"],
    "suburban": ["thun", "st-gallen"],
    "small": ["murten", "glarus"],
    "rural": ["appenzell"],
    "alpine": ["scuol", "adelboden"],
    "mid": ["st-gallen", "lugano"],
}


@dataclass
class LayerMeta:
    p5: float
    p95: float
    higher_is_better: bool


def fetch_json(url: str) -> dict[str, Any] | None:
    try:
        with urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except (URLError, TimeoutError, json.JSONDecodeError) as err:
        print(f"  warn: {url}: {err}")
        return None


def normalize_t(raw: float, meta: LayerMeta) -> float:
    p5, p95 = meta.p5, meta.p95
    if not math.isfinite(raw):
        return float("nan")
    if p95 <= p5:
        return 1.0 if (raw >= p5) == meta.higher_is_better else 0.0
    linear = min(1.0, max(0.0, (raw - p5) / (p95 - p5)))
    return linear if meta.higher_is_better else 1.0 - linear


def lv95_index(lng: float, lat: float, x_coord: np.ndarray, y_coord: np.ndarray) -> tuple[int, int]:
    x, y = WGS84_TO_LV95.transform(lng, lat)
    xi = int(np.argmin(np.abs(x_coord - x)))
    yi = int(np.argmin(np.abs(y_coord - y)))
    return yi, xi


def sample_layer(layer: dict[str, Any], lng: float, lat: float) -> float | None:
    store_url = f"{ZARR_BASE}/{layer['store']}"
    meta_url = f"{store_url}/{META_NAME}"
    meta_json = fetch_json(meta_url)
    if meta_json:
        meta = LayerMeta(
            p5=float(meta_json["p5"]),
            p95=float(meta_json["p95"]),
            higher_is_better=bool(meta_json["higherIsBetter"]),
        )
    else:
        clim = layer["clim"]
        meta = LayerMeta(p5=clim[0], p95=clim[1], higher_is_better=layer["hib"])

    try:
        ds = xr.open_zarr(store_url, consolidated=True)
        da = ds[layer["var"]]
        if "band" in da.dims and da.sizes.get("band", 1) == 1:
            da = da.isel(band=0)
        yi, xi = lv95_index(lng, lat, da["x"].values, da["y"].values)
        val = float(da.isel(y=yi, x=xi).values)
        if not math.isfinite(val):
            return None
        return val
    except Exception as err:  # pragma: no cover
        print(f"  zarr skip {layer['id']}: {err}")
        return None


def suggest_plateau(group_ts: list[float], higher_is_better: bool) -> tuple[float, float]:
    arr = np.array([t for t in group_ts if math.isfinite(t)])
    if arr.size == 0:
        return 0.35, 0.65
    p25, p75 = np.percentile(arr, [25, 75])
    if not higher_is_better:
        # prefer lower raw → lower t; plateau on low t side
        return max(0.05, p25 - 0.08), min(0.85, p75 + 0.05)
    return max(0.05, p25 - 0.05), min(0.92, p75 + 0.08)


def main() -> None:
    print("Sampling layers at", len(CITIES), "places (may take a few minutes)…")
    profiles: dict[str, dict[str, Any]] = {}
    layer_meta_cache: dict[str, LayerMeta] = {}

    for city in CITIES:
        cid = city["id"]
        profiles[cid] = {"name": city["name"], "archetype": city["archetype"], "layers": {}}
        print(f"  {city['name']}…")
        for layer in LAYERS:
            lid = layer["id"]
            raw = sample_layer(layer, city["lng"], city["lat"])
            meta_url = f"{ZARR_BASE}/{layer['store']}/{META_NAME}"
            if lid not in layer_meta_cache:
                mj = fetch_json(meta_url)
                if mj:
                    layer_meta_cache[lid] = LayerMeta(
                        float(mj["p5"]), float(mj["p95"]), bool(mj["higherIsBetter"])
                    )
                else:
                    layer_meta_cache[lid] = LayerMeta(
                        layer["clim"][0], layer["clim"][1], layer["hib"]
                    )
            meta = layer_meta_cache[lid]
            t = normalize_t(raw, meta) if raw is not None else float("nan")
            profiles[cid]["layers"][lid] = {
                "raw": raw,
                "t": None if not math.isfinite(t) else round(t, 3),
            }

    # Aggregate by archetype
    layer_stats: dict[str, Any] = {}
    for layer in LAYERS:
        lid = layer["id"]
        meta = layer_meta_cache[lid]
        by_arch: dict[str, list[float]] = {}
        all_t: list[float] = []
        for arch, city_ids in ARCHETYPE_GROUPS.items():
            ts = [
                profiles[c]["layers"][lid]["t"]
                for c in city_ids
                if c in profiles and profiles[c]["layers"].get(lid, {}).get("t") is not None
            ]
            by_arch[arch] = ts
            all_t.extend(ts)
        urban_ts = by_arch.get("urban", [])
        rural_ts = by_arch.get("rural", []) + by_arch.get("alpine", [])
        p_lo, p_hi = suggest_plateau(all_t, meta.higher_is_better)
        urban_mean = float(np.mean(urban_ts)) if urban_ts else float("nan")
        rural_mean = float(np.mean(rural_ts)) if rural_ts else float("nan")
        layer_stats[lid] = {
            "higherIsBetter": meta.higher_is_better,
            "p5": meta.p5,
            "p95": meta.p95,
            "national_t_p25": float(np.percentile(all_t, 25)) if all_t else None,
            "national_t_p75": float(np.percentile(all_t, 75)) if all_t else None,
            "urban_mean_t": urban_mean,
            "rural_mean_t": rural_mean,
            "suggested_plateau": [round(p_lo, 2), round(p_hi, 2)],
            "by_archetype": {k: [round(x, 2) for x in v] for k, v in by_arch.items()},
        }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {"cities": profiles, "layer_stats": layer_stats, "generated_by": "analyze_settlement_profiles.py"}
    OUT_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    md_lines = [
        "# Preset statistics — settlement factor profiles",
        "",
        "Generated by `data-pipelines/analyze_settlement_profiles.py` from GeoZarr samples",
        f"at {len(CITIES)} representative places.",
        "",
        "## City samples (preference scale t)",
        "",
        "| City | Archetype | " + " | ".join(L["id"][:12] for L in LAYERS[:6]) + " |",
        "|------|-----------|" + "|".join(["---"] * 6) + "|",
    ]
    for city in CITIES[:8]:
        row = [city["name"], city["archetype"]]
        for layer in LAYERS[:6]:
            t = profiles[city["id"]]["layers"].get(layer["id"], {}).get("t")
            row.append("—" if t is None else f"{t:.2f}")
        md_lines.append("| " + " | ".join(row) + " |")
    md_lines.extend(["", "## Suggested plateaus per layer (t scale)", ""])
    md_lines.append("| Layer | higherIsBetter | urban mean t | rural/alpine mean | plateau [lo, hi] |")
    md_lines.append("|-------|----------------|--------------|-------------------|------------------|")
    for layer in LAYERS:
        st = layer_stats[layer["id"]]
        pl = st["suggested_plateau"]
        md_lines.append(
            f"| {layer['id']} | {st['higherIsBetter']} | "
            f"{st['urban_mean_t']:.2f} | {st['rural_mean_t']:.2f} | [{pl[0]}, {pl[1]}] |"
        )

    md_lines.extend(
        [
            "",
            "## Preset design notes",
            "",
            "- **Urban & ÖV**: Plateau ÖV/quality on upper t where cities score; strict traffic dealbreakers.",
            "- **Quiet & green**: Plateau tranquillity/green on high t in rural samples; density plateau low t.",
            "- **Car-oriented**: Plateau MIV accessibility/travel on urban car-friendly t (mid-high for time = short).",
            "- **Family**: Mid density band; ÖV and tranquillity moderately high importance.",
            "- **Balanced**: National p25–p75 plateaus with soft floors (0.2).",
            "",
            "See `data-pipelines/output/city_factor_profiles.json` for full numeric export.",
        ]
    )
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_MD}")


if __name__ == "__main__":
    main()
