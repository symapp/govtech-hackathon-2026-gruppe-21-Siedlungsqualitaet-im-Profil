import type { QueryResult } from '@carbonplan/zarr-layer';
import { OVERVIEW_COLORMAP } from '../config/zarr-layers.config';
import type { ZarrLayerDefinition } from '../config/zarr-layers.config';
import type { LayerPreference } from '../models/layer-preference.model';
import type { SettlementLayerMeta } from '../models/settlement-layer-meta.model';
import type { OverviewLodPlan } from '../utils/overview-lod.util';
import {
  cellExtentToImageCoordinates,
  lv95ToCellIndex,
  viewportCellExtent,
  type ViewportCellExtent,
} from '../utils/swiss-grid.util';
import {
  climToNormalizationBounds,
  factorScoreFromRaw,
  metaToNormalizationBounds,
} from '../utils/preference-scoring.util';
import { OverviewRawCache } from './overview-raw-cache';
import { queryOverviewCellMap, type OverviewQueryContext } from './overview-cell-query';

const PARALLEL_LAYER_FETCHES = 3;
const OVERVIEW_CANVAS_SCALE = 2;

export interface OverviewCompositeSource {
  definition: ZarrLayerDefinition;
  ready: boolean;
  queryContext: OverviewQueryContext;
}

export interface OverviewCompositeInput {
  sources: OverviewCompositeSource[];
  preferences: Readonly<Record<string, LayerPreference>>;
  metaByLayerId: Readonly<Record<string, SettlementLayerMeta | null>>;
  west: number;
  south: number;
  east: number;
  north: number;
  zoom: number;
  plan: OverviewLodPlan;
  rawCache: OverviewRawCache;
  signal?: AbortSignal;
  onFetchLayer?: () => void;
}

export interface OverviewCompositeResult {
  canvas: HTMLCanvasElement;
  coordinates: [[number, number], [number, number], [number, number], [number, number]];
  extent: ViewportCellExtent;
}

export interface ScoreOverviewInput {
  rawByLayerId: ReadonlyMap<string, Map<string, number>>;
  sources: OverviewCompositeSource[];
  preferences: Readonly<Record<string, LayerPreference>>;
  metaByLayerId: Readonly<Record<string, SettlementLayerMeta | null>>;
  extent: ViewportCellExtent;
}

function scoreToRgb(score: number): [number, number, number, number] {
  const t = Math.min(1, Math.max(0, score / 100));
  const stops = OVERVIEW_COLORMAP;
  const idx = t * (stops.length - 1);
  const i0 = Math.floor(idx);
  const i1 = Math.min(stops.length - 1, i0 + 1);
  const f = idx - i0;
  const parse = (hex: string) => {
    const h = hex.replace('#', '');
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)] as const;
  };
  const c0 = parse(stops[i0]);
  const c1 = parse(stops[i1]);
  return [
    Math.round(c0[0] + (c1[0] - c0[0]) * f),
    Math.round(c0[1] + (c1[1] - c0[1]) * f),
    Math.round(c0[2] + (c1[2] - c0[2]) * f),
    220,
  ];
}

function smoothOverviewCanvas(sourceCanvas: HTMLCanvasElement): HTMLCanvasElement {
  if (OVERVIEW_CANVAS_SCALE <= 1) {
    return sourceCanvas;
  }

  const blurredCanvas = document.createElement('canvas');
  blurredCanvas.width = sourceCanvas.width;
  blurredCanvas.height = sourceCanvas.height;

  const blurredCtx = blurredCanvas.getContext('2d', { alpha: true });
  if (!blurredCtx) {
    return sourceCanvas;
  }

  blurredCtx.imageSmoothingEnabled = true;
  blurredCtx.imageSmoothingQuality = 'medium';
  const filter = 'blur(0.9px)';
  try {
    blurredCtx.filter = filter;
    if (blurredCtx.filter !== filter) {
      return sourceCanvas;
    }
    blurredCtx.clearRect(0, 0, blurredCanvas.width, blurredCanvas.height);
    blurredCtx.drawImage(sourceCanvas, 0, 0);
  } catch {
    return sourceCanvas;
  } finally {
    blurredCtx.filter = 'none';
  }

  const outputCanvas = document.createElement('canvas');
  outputCanvas.width = blurredCanvas.width * OVERVIEW_CANVAS_SCALE;
  outputCanvas.height = blurredCanvas.height * OVERVIEW_CANVAS_SCALE;

  const outputCtx = outputCanvas.getContext('2d', { alpha: true });
  if (!outputCtx) {
    return sourceCanvas;
  }

  outputCtx.imageSmoothingEnabled = true;
  outputCtx.imageSmoothingQuality = 'medium';
  outputCtx.clearRect(0, 0, outputCanvas.width, outputCanvas.height);
  outputCtx.drawImage(blurredCanvas, 0, 0, outputCanvas.width, outputCanvas.height);

  return outputCanvas;
}

/** Parse region query (proj4 LV95 x/y coords) into hectare cell map. */
export function parseRegionQueryToCellMap(
  result: QueryResult,
  variable: string,
): Map<string, number> {
  const map = new Map<string, number>();
  const values = result[variable];
  if (!Array.isArray(values)) {
    return map;
  }

  const coords = result.coordinates;
  const xs = (coords['x'] ?? coords['lon']) as number[];
  const ys = (coords['y'] ?? coords['lat']) as number[];
  if (!xs?.length || xs.length !== values.length) {
    return map;
  }

  for (let i = 0; i < values.length; i++) {
    const raw = values[i];
    if (typeof raw !== 'number' || !Number.isFinite(raw)) {
      continue;
    }
    const cell = lv95ToCellIndex(xs[i], ys[i]);
    if (!cell) {
      continue;
    }
    map.set(`${cell.ix},${cell.iy}`, raw);
  }

  return map;
}

export function viewportBboxPolygon(
  west: number,
  south: number,
  east: number,
  north: number,
): {
  type: 'Polygon';
  coordinates: number[][][];
} {
  return {
    type: 'Polygon',
    coordinates: [
      [
        [west, south],
        [east, south],
        [east, north],
        [west, north],
        [west, south],
      ],
    ],
  };
}

export function preferencesFingerprint(
  preferences: Readonly<Record<string, LayerPreference>>,
  layerIds: readonly string[],
): string {
  return layerIds
    .slice()
    .sort()
    .map((id) => {
      const p = preferences[id];
      if (!p?.enabled || p.importance <= 0) {
        return `${id}:off`;
      }
      return [
        id,
        p.enabled ? 1 : 0,
        p.importance,
        p.rangeMin.toFixed(4),
        p.rangeMax.toFixed(4),
        p.falloffLeft.toFixed(4),
        p.falloffRight.toFixed(4),
      ].join(':');
    })
    .join('|');
}

export function extentCacheKey(extent: ViewportCellExtent): string {
  return `${extent.ix0},${extent.iy0},${extent.ix1},${extent.iy1},${extent.stride}`;
}

export async function fetchOverviewRawMaps(
  input: OverviewCompositeInput,
  extent: ViewportCellExtent,
): Promise<Map<string, Map<string, number>>> {
  const active = input.sources.filter((s) => {
    const pref = input.preferences[s.definition.id];
    return s.ready && pref?.enabled && pref.importance > 0;
  });

  const result = new Map<string, Map<string, number>>();
  const queue = [...active];
  const { signal, plan, rawCache } = input;

  async function fetchOne(source: OverviewCompositeSource): Promise<void> {
    if (signal?.aborted) {
      return;
    }
    const { definition } = source;
    const cacheKey = OverviewRawCache.rawKey(
      plan.tier,
      definition.id,
      extent.ix0,
      extent.iy0,
      extent.ix1,
      extent.iy1,
      extent.stride,
    );
    const cached = rawCache.get(cacheKey);
    if (cached) {
      result.set(definition.id, cached);
      return;
    }

    try {
      const cellMap = await queryOverviewCellMap(
        source.queryContext,
        input.west,
        input.south,
        input.east,
        input.north,
        extent,
        signal,
      );

      if (signal?.aborted) {
        return;
      }

      rawCache.set(cacheKey, cellMap);
      result.set(definition.id, cellMap);
      input.onFetchLayer?.();
    } catch (err) {
      if (!signal?.aborted) {
        console.error(`[overview] failed to fetch layer ${definition.id}`, err);
      }
    }
  }

  async function worker(): Promise<void> {
    while (queue.length > 0) {
      if (signal?.aborted) {
        return;
      }
      const source = queue.shift();
      if (source) {
        await fetchOne(source);
      }
    }
  }

  await Promise.all(
    Array.from({ length: Math.min(PARALLEL_LAYER_FETCHES, active.length) }, () => worker()),
  );

  return result;
}

export function scoreOverviewComposite(
  input: ScoreOverviewInput,
): OverviewCompositeResult | null {
  const active = input.sources.filter((s) => {
    const pref = input.preferences[s.definition.id];
    return s.ready && pref?.enabled && pref.importance > 0;
  });

  if (active.length === 0) {
    return null;
  }

  const { extent } = input;
  const { ix0, iy0, stride, nx, ny } = extent;
  const scoreSum = new Float32Array(nx * ny);
  const scoreWeight = new Float32Array(nx * ny);

  for (const source of active) {
    const pref = input.preferences[source.definition.id]!;
    const meta = input.metaByLayerId[source.definition.id];
    const boundsNorm = meta
      ? metaToNormalizationBounds(meta, source.definition.higherIsBetter)
      : climToNormalizationBounds(source.definition.clim, source.definition.higherIsBetter);

    const cellMap = input.rawByLayerId.get(source.definition.id);
    if (!cellMap) {
      continue;
    }

    for (let py = 0; py < ny; py++) {
      for (let px = 0; px < nx; px++) {
        const ix = ix0 + px * stride;
        const iy = iy0 + py * stride;
        const raw = cellMap.get(`${ix},${iy}`);
        if (raw === undefined) {
          continue;
        }

        const score = factorScoreFromRaw(raw, boundsNorm, pref);
        const idx = py * nx + px;
        scoreSum[idx] += score * pref.importance;
        scoreWeight[idx] += pref.importance;
      }
    }
  }

  const canvas = document.createElement('canvas');
  canvas.width = nx;
  canvas.height = ny;
  const ctx = canvas.getContext('2d', { alpha: true });
  if (!ctx) {
    return null;
  }

  const imageData = ctx.createImageData(nx, ny);
  for (let py = 0; py < ny; py++) {
    for (let px = 0; px < nx; px++) {
      const idx = py * nx + px;
      const o = idx * 4;
      const w = scoreWeight[idx];
      if (w <= 0) {
        imageData.data[o + 3] = 0;
        continue;
      }
      const [r, g, b, a] = scoreToRgb(scoreSum[idx] / w);
      imageData.data[o] = r;
      imageData.data[o + 1] = g;
      imageData.data[o + 2] = b;
      imageData.data[o + 3] = a;
    }
  }

  ctx.putImageData(imageData, 0, 0);

  return {
    canvas: smoothOverviewCanvas(canvas),
    coordinates: cellExtentToImageCoordinates(extent),
    extent,
  };
}

export async function buildOverviewComposite(
  input: OverviewCompositeInput,
): Promise<OverviewCompositeResult | null> {
  const extent = viewportCellExtent(input.west, input.south, input.east, input.north);
  if (!extent) {
    return null;
  }

  const rawByLayerId = await fetchOverviewRawMaps(input, extent);
  if (input.signal?.aborted) {
    return null;
  }

  return scoreOverviewComposite({
    rawByLayerId,
    sources: input.sources,
    preferences: input.preferences,
    metaByLayerId: input.metaByLayerId,
    extent,
  });
}

/** @deprecated */
export function overviewImageCoordinates(
  west: number,
  south: number,
  east: number,
  north: number,
): [[number, number], [number, number], [number, number], [number, number]] {
  const extent = viewportCellExtent(west, south, east, north);
  if (!extent) {
    return [
      [west, north],
      [east, north],
      [east, south],
      [west, south],
    ];
  }
  return cellExtentToImageCoordinates(extent);
}
