# Siedlungsqualität im Profil

Siedlungsqualität im Profil is an interactive web application designed to evaluate and visualize Livability scores across Switzerland. It allows users to define their own living preferences using various indicators—such as noise levels, public transport accessibility, and population density—and see a personalized quality score directly on a map.

The project was developed as part of the BIT Hackathon.

---

## Key Features

- **Interactive Map**: Visualize high-resolution spatial data layers using MapLibre and Deck.gl.
- **Personalized Preferences**: Adjust scoring functions for each factor using an intuitive trapezoid editor.
- **Dynamic Scoring**: See a real-time weighted "Overview Score" based on your custom importance settings.
- **Nearby Amenities Integration**: Toggle infrastructure locations (grocery stores, doctors, pharmacies, theaters) nearby with personalized count tracking for regions of interest.
- **Multi-language Support**: Full support for German, French, Italian, and English.
- **High-Performance Data**: Leverages GeoZarr for efficient streaming of massive raster datasets directly in the browser.

---

## Tech Stack

### Frontend
- **Framework**: Angular (latest signals-based architecture)
- **Mapping**: MapLibre GL JS and Deck.gl
- **Data Rendering**: @carbonplan/zarr-layer (GeoZarr rendering)
- **Styling**: Vanilla SCSS (Swiss Design System inspired)
- **I18n**: @ngx-translate/core

### Data Pipelines
- **Language**: Python
- **Package Manager**: uv
- **Storage**: GeoZarr (EPSG:2056, 100m grid)
- **Object Storage**: Local MinIO bucket (S3-compatible)

---

## Getting Started

### 1. Frontend Development

```bash
cd frontend
npm install
npm start
```
The application will be available at http://localhost:4200.

### 2. Data Pipelines

The pipelines process raw Swiss administrative data (ARE, BAFU, BFS) into normalized GeoZarr layers.

```bash
# Setup environment and run all pipelines
cd data-pipelines
uv run python run-all-pipelines.py --upload
```
Note: Set MinIO/S3 credentials in a .env file at the root (see .env.example).

---

## Documentation

- **[Indicators reference](./INDICATORS.md)** — all factors: data sources, pipeline calculations, units, and scoring
- **[Technical Guide](./TECHNICAL_GUIDE.md)** — architecture, data grid, and map stack

---

## Contributors

- Group 21 (BIT Hackathon)
