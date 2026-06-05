# Indicators reference

This document describes every settlement-quality **factor** (indicator) used in *Siedlungsqualität im Profil*: where the data comes from, how it is processed into GeoZarr layers, what the stored values mean, and how the app turns them into user scores.

For architecture and map stack details, see [TECHNICAL_GUIDE.md](./TECHNICAL_GUIDE.md). For running pipelines, see [data-pipelines/README.md](./data-pipelines/README.md).

---

## Shared spatial grid

All raster indicators share one national grid so layers stack pixel-perfectly in the browser:

| Property | Value |
|----------|--------|
| CRS | EPSG:2056 (CH1903+ / LV95) |
| Cell size | 100 m |
| Extent (outer edges) | E 2 485 400–2 833 000, N 1 075 200–1 296 000 |
| Storage | GeoZarr on local MinIO (S3-compatible) |
| Coordinates in Zarr | Cell **centers**; `y` decreases northward |

Defined in `data-pipelines/are_rasterize_lib.py` (`SWISS_GRID_100M_EDGE_BOUNDS`) and mirrored in `frontend/src/app/config/zarr-layers.config.ts`.

Coarser overview tiles (500 m / 1000 m) are produced by `coarsen_settlement_layers.py` for the national composite map.

---

## How scores are calculated in the app

Indicators are **not** pre-scored for the user. The app reads **raw** cell values from GeoZarr and applies personal preferences.

### Step 1 — Normalize raw value to preference scale `t ∈ [0, 1]`

For each factor, `t` is derived from the cell’s raw value and layer metadata:

- Prefer `settlement-layer-meta.json` next to each Zarr store (`p5`, `p95`, `higherIsBetter`).
- If meta is missing, fall back to fixed `clim` bounds in `zarr-layers.config.ts`.

```text
linear = clamp((raw - p5) / (p95 - p5), 0, 1)
t = higherIsBetter ? linear : (1 - linear)
```

(`frontend/src/app/utils/preference-scoring.util.ts` — `normalizeToPreferenceScale`)

Percentile bounds in meta are computed on valid raster cells with a default **5th / 95th** percentile cutoff (`settlement_layer_meta.compute_percentile_bounds`).

### Step 2 — Trapezoid preference function → factor score `0–100`

The user sets a trapezoid on the **preference scale** (not raw units):

- **Plateau** `[rangeMin, rangeMax]`: score = **100**
- **Left falloff**: linear drop from 100 to 0 over `falloffLeft`
- **Right falloff**: linear drop from 100 to 0 over `falloffRight`
- Outside the trapezoid: score = **0**

Default trapezoids and importances: `frontend/src/app/config/good-place-defaults.config.ts`.

### Step 3 — Overview score (Gesamtübersicht)

Weighted mean over **enabled** factors with `importance > 0`:

```text
Overview = Σ (factorScore × importance) / Σ importance
```

Missing values at the clicked location are skipped. Same logic drives the precomputed overview raster (`settlement-overview-composite` service).

### Region of interest

For a circular region, factor values are **averaged** over cells in the radius before scoring (`metrics-aggregate.util.ts`).

---

## Summary table

| UI id | Display name (DE) | Provider | Pipeline | Zarr variable | Unit | Higher raw = better? (app) |
|-------|-------------------|----------|----------|---------------|------|----------------------------|
| `tranquillity` | Ruhe | BAFU | `tranquillity-rasterize.py` | `tranquillity_index` | index 0–1 | yes |
| `population-density` | Bevölkerungsdichte | BFS | `density-rasterize.py` | `population_density_score` | Einw./km² | **no** |
| `pt-accessibility` | ÖV-Erreichbarkeit | ARE | `rasterize-are-metrics.py` / `accessibility-pt-rasterize.py` | `OeV_Erreichb_EW` | EW | yes |
| `miv-accessibility` | Strassen-Erreichbarkeit | ARE | `rasterize-are-metrics.py` | `Strasse_Erreichb_EW` | EW | yes* |
| `pt-quality` | ÖV-Güteklassen | ARE | `rasterize-are-metrics.py` | `KLASSE_NUM` | 1–4 | yes |
| `pt-travel-time` | Reisezeit ÖV | ARE | `rasterize-are-metrics.py` | `OeV_Reisezeit_Z` | min | **no** |
| `miv-travel-time` | Reisezeit Auto | ARE | `rasterize-are-metrics.py` | `Strasse_Reisezeit_Z` | min | **no** |
| `rail-traffic` | Bahn-Belastung | ARE | `rasterize-are-metrics.py` | `DTV_OEV` | DTV | **no** |
| `road-traffic` | Strassenverkehr | ARE | `rasterize-are-metrics.py` | `DTV_FZG` | DTV | **no** |
| `secondary-homes` | Zweitwohnungsanteil | ARE | `rasterize-are-metrics.py` | `ZWG_3110` | % | **no** |
| `landscape-type` | Landschaftstyp | ARE | `rasterize-are-metrics.py` | `TYP_NR` | type id | yes |
| `solar-potential` | Solar-Potenzial | ARE | `rasterize-are-metrics.py` | `solar_suitability` | 1–5 | yes |
| `tlm-green-trees` | Grünflächen & Bäume | swisstopo | `tlm-green-trees-rasterize.py` | `green_amenity_index` | 0–1 | yes |
| *(Lädeli)* | Lädeli | OpenStreetMap | live Overpass API | — | count | not in overview |

\*See [Strassen-Erreichbarkeit](#miv-accessibility-strassen-erreichbarkeit) — pipeline meta marks this factor as “lower is better” while the frontend config uses `higherIsBetter: true`. Defaults in `good-place-defaults.config.ts` favour **lower** accessibility values.

Frontend wiring: `frontend/src/app/config/zarr-layers.config.ts`. Registry & download URLs: `data-pipelines/are_metrics_registry.py`.

---

## Per-indicator details

### Tranquillity (`tranquillity`) — Ruhe

| | |
|--|--|
| **Source** | BAFU *Lärmempfindlichkeitskarte* / tranquillity map |
| **Access** | [geo.admin.ch STAC](https://data.geo.admin.ch/api/stac/v1/) collection `ch.bafu.tranquillity-karte` → Cloud Optimized GeoTIFF |
| **Pipeline** | `tranquillity-rasterize.py` — stream COG, reproject to 100 m grid (`Resampling.nearest`), mask nodata |
| **Output** | `ch_bafu_tranquillity_karte.zarr` |
| **Variable** | `tranquillity_index` — **raw index** from BAFU (typically 0–1; higher = quieter / better) |
| **Scoring direction** | Higher is better |

---

### Population density (`population-density`) — Bevölkerungsdichte

| | |
|--|--|
| **Source** | BFS **STATPOP** geodata (hectare grid, LV95 coordinates) |
| **Access** | BFS DAM API (`density-rasterize.py` → `ag-b-00.03-vz…` assets) |
| **Pipeline** | `density-rasterize.py` |
| **Calculation** | For each STATPOP 100 m cell: `population_density_per_km2 = population / cell_area_km2` with `cell_area_km2 = 0.01` (100 m × 100 m). Coordinates in CSV are **south-west corners** of cells; indexing accounts for that. |
| **Output** | `statpop_population_density_100m.zarr` |
| **Variables** | `population_density_score` (used by app), `population` (counts, auxiliary) |
| **Scoring direction** | Lower density is better for typical “livability score” preferences |

---

### PT accessibility (`pt-accessibility`) — ÖV-Erreichbarkeit

| | |
|--|--|
| **Source** | ARE *Erreichbarkeit ÖV* |
| **URL** | `https://data.geo.admin.ch/ch.are.erreichbarkeit-oev/.../erreichbarkeit-oev_2056.gpkg` |
| **Field** | `OeV_Erreichb_EW` (Erreichbarkeitswert, weighted accessibility to services) |
| **Pipeline** | `rasterize-are-metrics.py` (or legacy `accessibility-pt-rasterize.py`) — vector → 100 m via `geocube` |
| **Output** | `erreichbarkeit_swiss_grid_100m.zarr` |
| **Scoring direction** | Higher EW is better (better public-transport access) |
| **Note** | Interior gaps may be filled with 0 before national overview scoring (`settlement_quality_scoring.py`, `fill_mode: nan_and_zero`) |

---

### MIV accessibility (`miv-accessibility`) — Strassen-Erreichbarkeit

| | |
|--|--|
| **Source** | ARE *Erreichbarkeit MIV* (motorised individual traffic) |
| **URL** | `https://data.geo.admin.ch/ch.are.erreichbarkeit-miv/.../erreichbarkeit-miv_2056.gpkg` |
| **Field** | `Strasse_Erreichb_EW` |
| **Output** | `erreichbarkeit_miv_swiss_grid_100m.zarr` |
| **Official meaning** | Higher EW = better road-network accessibility to services |
| **App config** | `higherIsBetter: true` in `zarr-layers.config.ts`, but sensible defaults prefer **low** normalized values; `metric_layer_meta.py` uses `higher_is_better=False` for meta generation |

---

### PT quality (`pt-quality`) — ÖV-Güteklassen

| | |
|--|--|
| **Source** | ARE *Güteklassen ÖV* (2026 dataset) |
| **URL** | `https://data.geo.admin.ch/ch.are.gueteklassen_oev/.../gueteklassen_oev_2026_2056.gpkg.zip` |
| **Layer** | `OeV_Gueteklassen_ARE` |
| **Transformation** | Letter class → numeric: A→4, B→3, C→2, D→1 (`are_metrics_registry._prepare_pt_quality`) |
| **Variable** | `KLASSE_NUM` (1–4) |
| **Output** | `pt_quality_swiss_grid_100m.zarr` |
| **Scoring direction** | Higher class number is better |

---

### PT travel time (`pt-travel-time`) — Reisezeit ÖV

| | |
|--|--|
| **Source** | ARE *Reisezeit ÖV* to six major centres |
| **URL** | `https://data.geo.admin.ch/ch.are.reisezeit-oev/.../reisezeit-oev_2056.gpkg` |
| **Layer** | `Reisezeit_Erreichbarkeit` |
| **Field** | `OeV_Reisezeit_Z` (minutes) |
| **Output** | `reisezeit_oev_swiss_grid_100m.zarr` |
| **Typical range** | ~15–90 min (used for overview scaling in `settlement_quality_scoring.py`) |
| **Scoring direction** | Shorter travel time is better |

---

### MIV travel time (`miv-travel-time`) — Reisezeit Auto

| | |
|--|--|
| **Source** | ARE *Reisezeit MIV* |
| **URL** | `https://data.geo.admin.ch/ch.are.reisezeit-miv/.../reisezeit-miv_2056.gpkg` |
| **Field** | `Strasse_Reisezeit_Z` (minutes) |
| **Output** | `reisezeit_miv_swiss_grid_100m.zarr` |
| **Typical range** | ~10–75 min |
| **Scoring direction** | Shorter is better |

---

### Rail traffic (`rail-traffic`) — Bahn-Belastung

| | |
|--|--|
| **Source** | ARE *Belastung Personenverkehr Bahn* |
| **URL** | `https://data.geo.admin.ch/ch.are.belastung-personenverkehr-bahn/...` |
| **Field** | `DTV_OEV` (daily passenger volume proxy along corridor) |
| **Rasterization** | Line geometries buffered by **200 m**, then rasterized |
| **Output** | `belastung_bahn_swiss_grid_100m.zarr` |
| **Scoring direction** | Lower load is better (quieter) |

---

### Road traffic (`road-traffic`) — Strassenverkehr

| | |
|--|--|
| **Source** | ARE *Belastung Personenverkehr Strasse* |
| **URL** | `https://data.geo.admin.ch/ch.are.belastung-personenverkehr-strasse/...` |
| **Layer** | `Personen_Gueterverkehr_Strasse` |
| **Field** | `DTV_FZG` (vehicles per day) |
| **Rasterization** | Line buffer **100 m** |
| **Output** | `belastung_strasse_swiss_grid_100m.zarr` |
| **Scoring direction** | Lower is better |

---

### Secondary homes (`secondary-homes`) — Zweitwohnungsanteil

| | |
|--|--|
| **Source** | ARE *Wohnungsinventar Zweitwohnungsanteil* (March 2026 vintage) |
| **URL** | `https://data.geo.admin.ch/ch.are.wohnungsinventar-zweitwohnungsanteil/...` |
| **Field** | `ZWG_3110` — share of secondary homes in **%** |
| **Output** | `zweitwohnungsanteil_swiss_grid_100m.zarr` |
| **Scoring direction** | Lower share is better (less tourist/second-home pressure) |

---

### Landscape type (`landscape-type`) — Landschaftstyp

| | |
|--|--|
| **Source** | ARE *Landschaftstypen* |
| **URL** | `https://data.geo.admin.ch/ch.are.landschaftstypen/.../landschaftstypen_2056.gpkg` |
| **Field** | `TYP_NR` — categorical landscape type ID (not an ordinal quality score by itself) |
| **Output** | `landschaftstypen_swiss_grid_100m.zarr` |
| **Scoring direction** | Treated as “higher type number = better” in the app for normalization; **disabled by default** in sensible presets |
| **Note** | Interpret type numbers using ARE’s landscape typology documentation |

---

### Solar potential (`solar-potential`) — Solar-Potenzial

| | |
|--|--|
| **Source** | ARE *Solaranlagen Nutzungsaspekte* |
| **URL** | `https://data.geo.admin.ch/ch.are.solaranlagen-nutzungsaspekte/.../solaranlagen-nutzungsaspekte_2056.tif` |
| **Pipeline** | COG → 100 m grid (`rasterize_from_cog`) |
| **Variable** | `solar_suitability` — suitability **class 1–5** (higher = more favourable) |
| **Output** | `solar_nutzungsaspekte.zarr` |
| **Scoring direction** | Higher class is better |

---

### TLM green & trees (`tlm-green-trees`) — Grünflächen & Bäume

| | |
|--|--|
| **Source** | swisstopo **swissTLM3D** (national GeoPackage, ~4.8 GiB; auto-download via STAC or local `--tiles-dir`) |
| **Pipeline** | `tlm-green-trees-rasterize.py` |
| **Green polygons** | `TLM_BODENBEDECKUNG` with Objektart ∈ {Gehölzfläche, Gebüschwald, Wald, Wald_offen, Feuchtgebiet} (codes 6, 11–14) |
| **Trees** | `TLM_EINZELBAUM_GEBUESCH` points with Objektart/ObjectVal = Einzelbaum (code 1) |
| **Per 100 m cell** | |
| | `green_area_fraction` = min(1, green_area_m² / 10 000) |
| | `single_tree_density_per_ha` = tree_count × 100 |
| | `green_amenity_index` = **0.5 × green_fraction + 0.5 × tree_norm**, where `tree_norm` = density / 95th percentile of positive densities (national), clipped to [0, 1] |
| **Output** | `tlm_green_trees_swiss_grid_100m.zarr` (app uses `green_amenity_index`) |
| **Attribution** | © swisstopo |
| **Scoring direction** | Higher index is better |

Config: `data-pipelines/tlm_green_trees_config.py`.

---

## Lädeli (grocery stores) — not a raster indicator

| | |
|--|--|
| **Source** | [OpenStreetMap](https://www.openstreetmap.org/) via **Overpass API** |
| **Query** | Nodes/ways/relations with `shop` ∈ `supermarket`, `grocery`, `convenience`, `greengrocer` within the user’s region radius |
| **Implementation** | `frontend/src/app/services/overpass.service.ts` |
| **Display** | Count and list in the sidebar; map icons via Deck.gl — **not** part of the weighted overview score or GeoZarr stack |
| **Caching** | Debounced per region in `LocationService` |

---

## Rasterization mechanics (ARE / BAFU vectors)

Shared library: `data-pipelines/are_rasterize_lib.py`.

1. Download GeoPackage or COG from geo.admin.ch (or BFS / STAC).
2. **Vectors**: `geocube.make_geocube` at 100 m resolution in EPSG:2056, then reindex to the canonical grid (`align_geocube_to_swiss_100m_grid`).
3. **Rasters**: `rioxarray` reproject/clip to the same grid (`align_raster_to_swiss_100m_grid`).
4. **Line layers** (traffic): buffer lines (100 m or 200 m) before rasterizing so corridors get width.
5. Write GeoZarr + optional `settlement-layer-meta.json` (`p5`, `p95`, `higherIsBetter`, `unit`).

Regenerate everything:

```bash
cd data-pipelines
uv run python run-all-pipelines.py --force --upload
```

Emit meta only for existing Zarr dirs:

```bash
uv run python emit_settlement_meta.py --data-dir .
```

---

## National overview composite (backend preprocessing)

`settlement_quality_scoring.py` defines how each layer is turned into a **0–1 quality score** for pre-rendered overview tiles (independent of user trapezoids):

- Linear scale using either fixed `scale_range` or 5th–95th percentiles.
- Invert when `higher_is_better` is false.
- Fill nodata / zero inside Switzerland’s border mask (`swissboundaries3d`).

Default weights for that composite differ from UI importances; see `QUALITY_SPECS` in the same file.

---

## Code index

| Topic | Location |
|-------|----------|
| Layer list, Zarr URLs, colormaps | `frontend/src/app/config/zarr-layers.config.ts` |
| Default trapezoids & importances | `frontend/src/app/config/good-place-defaults.config.ts` |
| Scoring math | `frontend/src/app/utils/preference-scoring.util.ts` |
| Overview aggregation | `frontend/src/app/utils/metrics-aggregate.util.ts` |
| ARE source registry | `data-pipelines/are_metrics_registry.py` |
| Meta units & higher-is-better (pipelines) | `data-pipelines/metric_layer_meta.py` |
| Percentile meta writer | `data-pipelines/settlement_layer_meta.py` |
| UI copy (DE/FR/IT/EN) | `frontend/public/i18n/*.json` → `layers.*` |

---

## Attribution

- **ARE** — Bundesamt für Raumentwicklung (accessibility, travel times, traffic, housing, landscape, solar)
- **BAFU** — Bundesamt für Umwelt (tranquillity / noise sensitivity)
- **BFS** — Bundesamt für Statistik (STATPOP population)
- **swisstopo** — swissTLM3D green/tree layers
- **OpenStreetMap** contributors — Lädeli data

Always verify licensing and citation requirements on the respective data portals when publishing derivatives.
