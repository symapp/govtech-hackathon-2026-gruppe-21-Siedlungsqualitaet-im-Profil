# Data Pipelines

This directory contains Python scripts for processing raw spatial data into normalized **GeoZarr** layers.

## 🛠 Prerequisites

- [uv](https://github.com/astral-sh/uv) (Python package manager)
- Source data in the `data/` directory (not committed to repo).

## 🚀 Usage

### Run All Pipelines
To regenerate and upload all layers to the configured S3-compatible bucket (local MinIO by default):

```bash
uv run python run-all-pipelines.py --force --upload
```

### Individual Indicators
You can run specific scripts for individual datasets:

```bash
# Population Density
uv run python density-rasterize.py --upload

# ARE Metrics (multiple indicators)
uv run python rasterize-are-metrics.py all-new --upload

# PT Quality
uv run python pt-quality-rasterize.py --upload

# Swiss TLM green areas & single-tree density (requires local tiles)
uv run python tlm-green-trees-rasterize.py --tiles-dir ../data/swisstlm3d --upload
```

### Swiss TLM: Grünflächen & Einzelbaumdichte

By default the script downloads the latest national **swissTLM3D** GeoPackage from [geo.admin.ch STAC](https://data.geo.admin.ch/api/stac/v1/collections/ch.swisstopo.swisstlm3d) into `data/swisstlm3d/` (not committed, ~4.8 GiB zip). You can also pass local tiles via `--tiles-dir`.

Feature filters (`tlm_green_trees_config.py`):

| Source | Feature class | Filter |
|--------|---------------|--------|
| Bodenbedeckung | `TLM_BODENBEDECKUNG` | Objektart ∈ Gehoelzflaeche, Gebueschwald, Wald, Wald_offen, Feuchtgebiet (codes 6, 11–14) |
| Einzelbäume | `TLM_EINZELBAUM_GEBUESCH` | ObjectVal / Objektart = Einzelbaum (code 1) |

Output: `tlm_green_trees_swiss_grid_100m.zarr` with `green_amenity_index` (0–1 composite of green-area share and normalized tree density per ha). Attribution: © swisstopo.

After the 100 m layer is built:

```bash
uv run python coarsen_settlement_layers.py --fine-dir . --layer-id tlm-green-trees
```

## 📖 Indicator documentation

Per-factor sources, formulas, and scoring semantics: **[../INDICATORS.md](../INDICATORS.md)**.

## 🏗 Key Components

- **`are_rasterize_lib.py`**: Shared utility functions for rasterizing ARE datasets to the standard 100m grid.
- **`settlement_layer_meta.py`**: Generates JSON metadata (`p5`, `p95`) for each layer to drive frontend normalization.
- **`zarr_b2_upload.py`**: Handles streaming upload of Zarr chunks to the configured S3-compatible bucket.

## 📐 Standard Grid (EPSG:2056)
Every script ensures the output Zarr matches the standard Swiss grid resolution and extent to allow pixel-perfect stacking in the frontend.
