# Technical Guide: Siedlungsqualität im Profil

This document provides a deep dive into the architecture, data processing, and scoring logic used in the project.

---

## 1. Data Foundation: The Swiss Grid

To ensure all spatial layers align perfectly without expensive on-the-fly reprojection, the project uses a standardized grid:

- **Projection**: EPSG:2056 (CH1903+ / LV95)
- **Resolution**: 100 meters per pixel.
- **Extent**: Covers the entirety of Switzerland (2485400–2833000 E / 1075200–1296000 N).
- **Format**: GeoZarr.

### Why GeoZarr?
Zarr allows the frontend to fetch only the specific chunks of data needed for the current map view. This enables smooth visualization of multi-gigabyte datasets without a backend server—data is streamed directly from object storage (local MinIO).

---

## 2. Frontend Architecture

The frontend is built with Angular 21 using a Signals-based reactive architecture.

### Map Stack
- **MapLibre GL JS**: Handles the base map (Carto Positron) and vector interactions.
- **Deck.gl**: A high-performance visualization framework that runs on top of MapLibre. It handles the rendering of Zarr raster layers and the nearby amenity icon overlays.
- **@carbonplan/zarr-layer**: A specialized Deck.gl layer that decodes and renders Zarr data on the GPU.

### State Management
We use Angular Signals for lightweight, granular state management:
- **LocationService**: Manages current coordinates, address search, and regions of interest.
- **ZarrMapService**: Orchestrates the loading and visibility of Zarr layers and samples data values for the current location.
- **LanguageService**: Handles i18n switching.

---

## 3. Scoring Logic: Preference Curves

For a full list of indicators (sources, raw values, pipeline steps, units), see **[INDICATORS.md](./INDICATORS.md)**.

Instead of fixed weights, every indicator (e.g., noise, density) uses a **piecewise-linear preference curve** on normalized scale `t ∈ [0, 1]` to calculate a factor score between 0 and 100.

### How it works:
1. **User Input**: The user defines plateau bounds, falloff widths, and optional floor/plateau heights via the curve editor (or lifestyle presets).
2. **Plateau**: Values between `rangeMin` and `rangeMax` receive `plateauFactor` (default 1 → 100 points).
3. **Linear Falloff**: Scores interpolate linearly from the plateau to `floorLeft` / `floorRight` outside the plateau (defaults 0.2 for exploratory home-search defaults).
4. **Extremes**: Values at or beyond the falloff anchors keep the floor factor (soft minimum, not always zero). **Dealbreaker** mode sets floors to 0.
5. **Lifestyle presets**: Curated shapes per use case (balanced, urban transit, quiet/green, car-oriented, family) stored in `lifestyle-presets.config.ts`.

### Weighted Overview Score:
The total quality score is a weighted average of all active indicator scores:
```
Total Score = Σ (Indicator Score * Importance) / Σ Importance
```

---

## 4. Nearby Amenities Integration

Infrastructure locations are fetched dynamically using the Overpass API (OpenStreetMap data).

- **Implementation**: When a region of interest is active, the `LocationService` queries Overpass for nodes tagged with `shop=supermarket`, `amenity=doctors`, `amenity=pharmacy`, etc., within the user-defined radius.
- **Visualization**: Amenities are rendered as customized map pin icons using a Deck.gl `IconLayer`.
- **Optimization**: Fetches are debounced and cached per region to minimize API calls.

---

## 5. Development Workflow

### Data Pipeline (Python)
The pipelines use xarray, rioxarray, and rasterio to:
1. Load raw GeoTIFF or CSV data.
2. Reproject to EPSG:2056.
3. Coarsen or interpolate to the 100m grid.
4. Calculate metadata (p5, p95 percentiles) for UI normalization.
5. Write and upload Zarr chunks.

### Frontend Patching
The project includes a patch for @carbonplan/zarr-layer to fix issues with BigInt coordinate handling and missing band dimensions in certain Zarr implementations. This is applied automatically via patch-package during npm install.

---

## 6. Projections and Calculations

The frontend performs coordinate transformations between WGS84 (used by MapLibre) and LV95 (used by the Zarr data) using the proj4 library.

- **Swiss Grid Utility**: See frontend/src/app/utils/swiss-grid.util.ts for logic related to cell indices and coordinate mapping.
