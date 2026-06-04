import type { Selector } from '@carbonplan/zarr-layer';
import { environment } from '../../environments/environment';
import type { LocationMetrics } from '../models/metrics.model';

/** LV95 / EPSG:2056 — matches pipeline GeoZarr outputs. */
export const SWISS_LV95_PROJ4 =
  '+proj=somerc +lat_0=46.9524055555556 +lon_0=7.43958333333333 +k_0=1 +x_0=2600000 +y_0=1200000 +ellps=bessel +units=m +no_defs';

export type ZarrMetricKey = keyof LocationMetrics;

/**
 * LV95 edge bounds [xMin, yMin, xMax, yMax] for the shared 100 m settlement-quality grid.
 * All GeoZarr layers (ARE, BAFU, BFS STATPOP) use this extent — see `are_rasterize_lib.SWISS_GRID_100M_EDGE_BOUNDS`.
 */
export const SWISS_GRID_LV95_BOUNDS: [number, number, number, number] = [
  2_485_400, 1_075_200, 2_833_000, 1_296_000,
];

export interface ZarrLayerDefinition {
  id: string;
  /** i18n key for the layer name, e.g. 'layers.tranquillity.label' */
  labelKey: string;
  /** i18n key for the layer description */
  descriptionKey: string;
  storePath: string;
  variable: string;
  selector?: Selector;
  /** LV95 edge bounds [xMin, yMin, xMax, yMax] in source CRS (meters). */
  bounds: [number, number, number, number];
  /** False when y decreases northward (shared settlement-quality grid). */
  latIsAscending: boolean;
  fillValue?: number;
  colormap: string[];
  clim: [number, number];
  /** Optional single-layer map opacity (defaults to service constant). */
  renderOpacity?: number;
  metricKey: ZarrMetricKey;
  /** i18n key for the metric label shown in the sidebar */
  metricLabelKey: string;
  /** i18n key for the metric unit */
  metricUnitKey: string;
  formatValue: (value: number) => string;
  /** Used for the aggregated overview score (0–100). */
  higherIsBetter: boolean;
  /** Set false for layers that are not compatible with overview grid/L0D yet. */
  includeInOverview?: boolean;
  /** Sidebar grouping for menu organization. Defaults to the main factors section. */
  sidebarGroup?: 'general' | 'weather';
  /** Coarse GeoZarr stores for national overview (from coarsen_settlement_layers.py). */
  overviewCoarse?: {
    storePath500: string;
    storePath1000: string;
    blockFactor500: number;
    blockFactor1000: number;
  };
}

function coarsePaths(fineStorePath: string): ZarrLayerDefinition['overviewCoarse'] {
  const base = fineStorePath.replace(/\.zarr\/?$/i, '');
  return {
    storePath500: `${base}_500m.zarr`,
    storePath1000: `${base}_1000m.zarr`,
    blockFactor500: 5,
    blockFactor1000: 10,
  };
}

export const DEFAULT_ACTIVE_ZARR_LAYER_ID = 'tranquillity';

export const OVERVIEW_MAP_LAYER_ID = 'settlement-quality-overview';

/** Colormap for the weighted composite overview layer (0 = low, 1 = high score). */
export const OVERVIEW_COLORMAP = ['#f7fcf5', '#c2e699', '#74c476', '#238b45', '#00441b'] as const;

export interface ZarrLayerGroup {
  id: 'general' | 'weather';
  titleKey?: string;
  layers: ZarrLayerDefinition[];
}

export function groupZarrLayerDefinitions(
  definitions: readonly ZarrLayerDefinition[] = ZARR_LAYER_DEFINITIONS,
): ZarrLayerGroup[] {
  const generalLayers = definitions.filter((definition) => definition.sidebarGroup !== 'weather');
  const weatherLayers = definitions.filter((definition) => definition.sidebarGroup === 'weather');
  const groups: ZarrLayerGroup[] = [{ id: 'general', layers: generalLayers }];

  if (weatherLayers.length > 0) {
    groups.push({ id: 'weather', titleKey: 'sidebar.weatherTitle', layers: weatherLayers });
  }

  return groups;
}

/**
 * Color scale limits (`clim`) — tune with `visualize-zarr.py` after uploading new layers.
 */
const CLIM = {
  // Emphasize user-facing weather bands: cold <15, mild 15-30, hot >30.
  temperature: [-20, 50] as [number, number],
  tranquillity: [0, 1] as [number, number],
  /** Raw Einw./km² from STATPOP (see settlement-layer-meta p5/p95 ≈ 300–9900). */
  populationDensity: [300, 9_900] as [number, number],
  vacancyRates: [0.5, 2.5] as [number, number],
  /** ARE ÖV EW (populated cells; see settlement-layer-meta p5/p95). */
  ptAccessibility: [4, 2_533] as [number, number],
  /** ARE MIV EW (populated cells; see settlement-layer-meta p5/p95). */
  roadAccessibility: [45, 11_657] as [number, number],
  ptQuality: [1, 4] as [number, number],
  ptTravelTime: [15, 90] as [number, number],
  roadTravelTime: [10, 75] as [number, number],
  railTraffic: [500, 25_000] as [number, number],
  roadTraffic: [500, 20_000] as [number, number],
  landscapeType: [1, 40] as [number, number],
  solarSuitability: [1, 5] as [number, number],
  /** From settlement-layer-meta p5/p95 on national swissTLM3D run. */
  greenAmenity: [0, 0.57] as [number, number],
} as const;

const base = environment.zarrBaseUrl;

const ZARR_LAYER_DEFINITIONS_BASE: Omit<ZarrLayerDefinition, 'overviewCoarse'>[] = [
  {
    id: 'temperature',
    labelKey: 'layers.temperature.label',
    descriptionKey: 'layers.temperature.description',
    storePath: `${base}/meteo_temperature_100m.zarr`,
    variable: 'temperature_celsius',
    bounds: SWISS_GRID_LV95_BOUNDS,
    latIsAscending: false,
    fillValue: Number.NaN,
    // Hard thermal thresholds: blue <15 C, green 15-30 C, red >30 C.
    colormap: [
      '#08306b',
      '#2171b5',
      '#4292c6',
      '#6baed6',
      '#9ecae1',
      '#238b45',
      '#41ab5d',
      '#74c476',
      '#a1d99b',
      '#c7e9c0',
      '#cb181d',
      '#ef3b2c',
      '#fb6a4a',
      '#fc9272',
      '#fcbba1',
    ],
    clim: CLIM.temperature,
    renderOpacity: 0.4,
    metricKey: 'temperatureCelsius',
    metricLabelKey: 'layers.temperature.metricLabel',
    metricUnitKey: 'layers.temperature.metricUnit',
    formatValue: (v) => v.toFixed(1),
    higherIsBetter: false,
    sidebarGroup: 'weather',
    includeInOverview: false,
  },
  {
    id: 'tranquillity',
    labelKey: 'layers.tranquillity.label',
    descriptionKey: 'layers.tranquillity.description',
    storePath: `${base}/ch_bafu_tranquillity_karte.zarr`,
    variable: 'tranquillity_index',
    bounds: SWISS_GRID_LV95_BOUNDS,
    latIsAscending: false,
    fillValue: Number.NaN,
    colormap: ['#440154', '#3b528b', '#21918c', '#5ec962', '#fde725'],
    clim: CLIM.tranquillity,
    metricKey: 'tranquillityIndex',
    metricLabelKey: 'layers.tranquillity.metricLabel',
    metricUnitKey: 'layers.tranquillity.metricUnit',
    formatValue: (v) => v.toFixed(2),
    higherIsBetter: true,
  },
  {
    id: 'population-density',
    labelKey: 'layers.populationDensity.label',
    descriptionKey: 'layers.populationDensity.description',
    storePath: `${base}/statpop_population_density_100m.zarr`,
    variable: 'population_density_score',
    bounds: SWISS_GRID_LV95_BOUNDS,
    latIsAscending: false,
    fillValue: Number.NaN,
    colormap: ['#ffffcc', '#fed976', '#fd8d3c', '#e31a1c', '#800026'],
    clim: CLIM.populationDensity,
    metricKey: 'populationDensityPerKm2',
    metricLabelKey: 'layers.populationDensity.metricLabel',
    metricUnitKey: 'layers.populationDensity.metricUnit',
    formatValue: (v) => Math.round(v).toLocaleString('de-CH'),
    higherIsBetter: false,
  },
  {
    id: 'vacancy-rates',
    labelKey: 'layers.vacancyRates.label',
    descriptionKey: 'layers.vacancyRates.description',
    storePath: `${base}/leerwohnungsziffer_municipalities_100m.zarr`,
    variable: 'leerwohnungsziffer',
    bounds: SWISS_GRID_LV95_BOUNDS,
    latIsAscending: false,
    fillValue: Number.NaN,
    colormap: ['#f7fcf0', '#ccebc5', '#7bccc4', '#2b8cbe', '#084081'],
    clim: CLIM.vacancyRates,
    metricKey: 'vacancyRatePercent',
    metricLabelKey: 'layers.vacancyRates.metricLabel',
    metricUnitKey: 'layers.vacancyRates.metricUnit',
    formatValue: (v) => `${v.toFixed(2)} %`,
    higherIsBetter: false,
    includeInOverview: false,
  },
  {
    id: 'pt-accessibility',
    labelKey: 'layers.ptAccessibility.label',
    descriptionKey: 'layers.ptAccessibility.description',
    storePath: `${base}/erreichbarkeit_swiss_grid_100m.zarr`,
    variable: 'OeV_Erreichb_EW',
    bounds: SWISS_GRID_LV95_BOUNDS,
    latIsAscending: false,
    fillValue: Number.NaN,
    colormap: ['#f7fbff', '#c6dbef', '#6baed6', '#2171b5', '#08306b'],
    clim: CLIM.ptAccessibility,
    metricKey: 'publicTransportAccessibility',
    metricLabelKey: 'layers.ptAccessibility.metricLabel',
    metricUnitKey: 'layers.ptAccessibility.metricUnit',
    formatValue: (v) => Math.round(v).toLocaleString('de-CH'),
    higherIsBetter: true,
  },
  {
    id: 'miv-accessibility',
    labelKey: 'layers.mivAccessibility.label',
    descriptionKey: 'layers.mivAccessibility.description',
    storePath: `${base}/erreichbarkeit_miv_swiss_grid_100m.zarr`,
    variable: 'Strasse_Erreichb_EW',
    bounds: SWISS_GRID_LV95_BOUNDS,
    latIsAscending: false,
    fillValue: Number.NaN,
    colormap: ['#fff5f0', '#fcbba1', '#fc9272', '#de2d26', '#67000d'],
    clim: CLIM.roadAccessibility,
    metricKey: 'roadAccessibility',
    metricLabelKey: 'layers.mivAccessibility.metricLabel',
    metricUnitKey: 'layers.mivAccessibility.metricUnit',
    formatValue: (v) => Math.round(v).toLocaleString('de-CH'),
    higherIsBetter: true,
  },
  {
    id: 'pt-quality',
    labelKey: 'layers.ptQuality.label',
    descriptionKey: 'layers.ptQuality.description',
    storePath: `${base}/pt_quality_swiss_grid_100m.zarr`,
    variable: 'KLASSE_NUM',
    bounds: SWISS_GRID_LV95_BOUNDS,
    latIsAscending: false,
    fillValue: Number.NaN,
    colormap: ['#d73027', '#fc8d59', '#91cf60', '#1a9850'],
    clim: CLIM.ptQuality,
    metricKey: 'publicTransportQuality',
    metricLabelKey: 'layers.ptQuality.metricLabel',
    metricUnitKey: 'layers.ptQuality.metricUnit',
    formatValue: (v) => v.toFixed(0),
    higherIsBetter: true,
  },
  {
    id: 'pt-travel-time',
    labelKey: 'layers.ptTravelTime.label',
    descriptionKey: 'layers.ptTravelTime.description',
    storePath: `${base}/reisezeit_oev_swiss_grid_100m.zarr`,
    variable: 'OeV_Reisezeit_Z',
    bounds: SWISS_GRID_LV95_BOUNDS,
    latIsAscending: false,
    fillValue: Number.NaN,
    colormap: ['#004529', '#41ab5d', '#fee08b', '#f46d43', '#a50026'],
    clim: CLIM.ptTravelTime,
    metricKey: 'publicTransportTravelTimeMin',
    metricLabelKey: 'layers.ptTravelTime.metricLabel',
    metricUnitKey: 'layers.ptTravelTime.metricUnit',
    formatValue: (v) => Math.round(v).toLocaleString('de-CH'),
    higherIsBetter: false,
  },
  {
    id: 'miv-travel-time',
    labelKey: 'layers.mivTravelTime.label',
    descriptionKey: 'layers.mivTravelTime.description',
    storePath: `${base}/reisezeit_miv_swiss_grid_100m.zarr`,
    variable: 'Strasse_Reisezeit_Z',
    bounds: SWISS_GRID_LV95_BOUNDS,
    latIsAscending: false,
    fillValue: Number.NaN,
    colormap: ['#004529', '#41ab5d', '#fee08b', '#f46d43', '#a50026'],
    clim: CLIM.roadTravelTime,
    metricKey: 'roadTravelTimeMin',
    metricLabelKey: 'layers.mivTravelTime.metricLabel',
    metricUnitKey: 'layers.mivTravelTime.metricUnit',
    formatValue: (v) => Math.round(v).toLocaleString('de-CH'),
    higherIsBetter: false,
  },
  {
    id: 'rail-traffic',
    labelKey: 'layers.railTraffic.label',
    descriptionKey: 'layers.railTraffic.description',
    storePath: `${base}/belastung_bahn_swiss_grid_100m.zarr`,
    variable: 'DTV_OEV',
    bounds: SWISS_GRID_LV95_BOUNDS,
    latIsAscending: false,
    fillValue: Number.NaN,
    colormap: ['#ffffb2', '#fecc5c', '#fd8d3c', '#f03b20', '#bd0026'],
    clim: CLIM.railTraffic,
    metricKey: 'railTrafficLoad',
    metricLabelKey: 'layers.railTraffic.metricLabel',
    metricUnitKey: 'layers.railTraffic.metricUnit',
    formatValue: (v) => Math.round(v).toLocaleString('de-CH'),
    higherIsBetter: false,
  },
  {
    id: 'road-traffic',
    labelKey: 'layers.roadTraffic.label',
    descriptionKey: 'layers.roadTraffic.description',
    storePath: `${base}/belastung_strasse_swiss_grid_100m.zarr`,
    variable: 'DTV_FZG',
    bounds: SWISS_GRID_LV95_BOUNDS,
    latIsAscending: false,
    fillValue: Number.NaN,
    colormap: ['#ffffb2', '#fecc5c', '#fd8d3c', '#f03b20', '#bd0026'],
    clim: CLIM.roadTraffic,
    metricKey: 'roadTrafficLoad',
    metricLabelKey: 'layers.roadTraffic.metricLabel',
    metricUnitKey: 'layers.roadTraffic.metricUnit',
    formatValue: (v) => Math.round(v).toLocaleString('de-CH'),
    higherIsBetter: false,
  },
  {
    id: 'landscape-type',
    labelKey: 'layers.landscapeType.label',
    descriptionKey: 'layers.landscapeType.description',
    storePath: `${base}/landschaftstypen_swiss_grid_100m.zarr`,
    variable: 'TYP_NR',
    bounds: SWISS_GRID_LV95_BOUNDS,
    latIsAscending: false,
    fillValue: Number.NaN,
    colormap: ['#8c510a', '#d8b365', '#5ab4ac', '#01665e', '#003c30'],
    clim: CLIM.landscapeType,
    metricKey: 'landscapeTypeId',
    metricLabelKey: 'layers.landscapeType.metricLabel',
    metricUnitKey: 'layers.landscapeType.metricUnit',
    formatValue: (v) => v.toFixed(0),
    higherIsBetter: true,
  },
  {
    id: 'solar-potential',
    labelKey: 'layers.solarPotential.label',
    descriptionKey: 'layers.solarPotential.description',
    storePath: `${base}/solar_nutzungsaspekte.zarr`,
    variable: 'solar_suitability',
    bounds: SWISS_GRID_LV95_BOUNDS,
    latIsAscending: false,
    fillValue: Number.NaN,
    colormap: ['#fff7bc', '#fec44f', '#d95f0e', '#993404'],
    clim: CLIM.solarSuitability,
    metricKey: 'solarSuitability',
    metricLabelKey: 'layers.solarPotential.metricLabel',
    metricUnitKey: 'layers.solarPotential.metricUnit',
    formatValue: (v) => v.toFixed(0),
    higherIsBetter: true,
  },
  {
    id: 'tlm-green-trees',
    labelKey: 'layers.tlmGreenTrees.label',
    descriptionKey: 'layers.tlmGreenTrees.description',
    storePath: `${base}/tlm_green_trees_swiss_grid_100m.zarr`,
    variable: 'green_amenity_index',
    bounds: SWISS_GRID_LV95_BOUNDS,
    latIsAscending: false,
    fillValue: Number.NaN,
    colormap: ['#f7fcf5', '#c2e699', '#74c476', '#238b45', '#00441b'],
    clim: CLIM.greenAmenity,
    metricKey: 'greenAmenityIndex',
    metricLabelKey: 'layers.tlmGreenTrees.metricLabel',
    metricUnitKey: 'layers.tlmGreenTrees.metricUnit',
    formatValue: (v) => v.toFixed(2),
    higherIsBetter: true,
  },
];

export const ZARR_LAYER_DEFINITIONS: ZarrLayerDefinition[] = ZARR_LAYER_DEFINITIONS_BASE.map(
  (definition) => ({
    ...definition,
    overviewCoarse: coarsePaths(definition.storePath),
  }),
);

/** Layers that use 0 as nodata in GeoZarr but should be treated as empty in the UI. */
export const ZARR_LAYERS_WITH_NAN_FILL = new Set(
  ZARR_LAYER_DEFINITIONS.filter((d) => d.fillValue === Number.NaN).map((d) => d.id),
);

export const DEFAULT_LAYER_WEIGHT = 100;

export function createDefaultLayerWeights(): Record<string, number> {
  return Object.fromEntries(ZARR_LAYER_DEFINITIONS.map((d) => [d.id, DEFAULT_LAYER_WEIGHT]));
}

export function createDefaultLayerEnabled(): Record<string, boolean> {
  return Object.fromEntries(ZARR_LAYER_DEFINITIONS.map((d) => [d.id, true]));
}
