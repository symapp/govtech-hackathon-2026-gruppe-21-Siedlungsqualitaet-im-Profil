import { Injectable, computed, inject, signal } from '@angular/core';
import { TranslateService } from '@ngx-translate/core';
import { ZarrLayer, type QueryResult } from '@carbonplan/zarr-layer';
import type { CustomLayerInterface, ImageSource, Map as MaplibreMap } from 'maplibre-gl';
import {
  createInitialLayerPreferences,
  createPreferencesForPreset,
  resetPreferencesForActivePreset,
  storeLifestylePresetId,
  type LifestylePresetId,
} from '../config/lifestyle-presets.config';
import {
  OVERVIEW_MAP_LAYER_ID,
  SWISS_LV95_PROJ4,
  ZARR_LAYER_DEFINITIONS,
  ZARR_LAYERS_WITH_NAN_FILL,
  type ZarrLayerDefinition,
} from '../config/zarr-layers.config';
import type { LayerPreference } from '../models/layer-preference.model';
import {
  settlementLayerMetaUrl,
  type SettlementLayerMeta,
} from '../models/settlement-layer-meta.model';
import { EMPTY_LOCATION_METRICS, type LocationMetrics } from '../models/metrics.model';
import { clampLayerPreference } from '../utils/preference-scoring.util';
import { computePreferenceOverview } from '../utils/metrics-aggregate.util';
import { overviewCompositeDebounceMs, resolveOverviewLod } from '../utils/overview-lod.util';
import {
  computeLayerDisplayPlan,
  isOverviewEligible,
  singleLayerOpacityFromImportance,
  singleLayerOpacityFromSlider,
} from '../utils/zarr-map-display.util';
import { SWITZERLAND_BBOX } from '../config/map-bounds.config';
import { cellExtentToImageCoordinates, viewportCellExtent } from '../utils/swiss-grid.util';
import type { ViewportCellExtent } from '../utils/swiss-grid.util';

/** Country zoom: overview uses the full settlement grid clip, not just map corner cells. */
const COUNTRY_OVERVIEW_MAX_ZOOM = 9;

function overviewExtentForMap(map: MaplibreMap): ViewportCellExtent | null {
  const bounds = map.getBounds();
  const west = bounds.getWest();
  const east = bounds.getEast();
  const south = bounds.getSouth();
  const north = bounds.getNorth();

  if (map.getZoom() <= COUNTRY_OVERVIEW_MAX_ZOOM) {
    return viewportCellExtent(
      SWITZERLAND_BBOX.west,
      SWITZERLAND_BBOX.south,
      SWITZERLAND_BBOX.east,
      SWITZERLAND_BBOX.north,
    );
  }

  return viewportCellExtent(west, south, east, north);
}

function overviewQueryBoundsForExtent(
  extent: ViewportCellExtent,
): { west: number; south: number; east: number; north: number } {
  const coordinates = cellExtentToImageCoordinates(extent);
  const west = Math.min(coordinates[0][0], coordinates[3][0]);
  const east = Math.max(coordinates[1][0], coordinates[2][0]);
  const north = Math.max(coordinates[0][1], coordinates[1][1]);
  const south = Math.min(coordinates[2][1], coordinates[3][1]);
  return { west, south, east, north };
}
import { OverviewRawCache } from './overview-raw-cache';
import {
  extentCacheKey,
  fetchOverviewRawMaps,
  preferencesFingerprint,
  scoreOverviewComposite,
  type OverviewCompositeResult,
} from './settlement-overview-composite';
import { exposeOverviewForE2e, type OverviewE2eState } from '../testing/e2e-overview.harness';
import { exposeZarrSampleForE2e } from '../testing/e2e-zarr.harness';
import { environment } from '../../environments/environment';
import type { MeteoManifest } from '../models/meteo-manifest.model';

interface ManagedZarrLayer {
  definition: ZarrLayerDefinition;
  layer: ZarrLayer;
  ready: boolean;
  loading: boolean;
}
export interface ZarrLayerState {
  id: string;
  labelKey: string;
  descriptionKey: string;
  colormap: string[];
  clim: [number, number];
  enabled: boolean;
  preference: LayerPreference;
  meta: SettlementLayerMeta | null;
  metaFromFallback: boolean;
  ready: boolean;
  loading: boolean;
}

const OVERVIEW_SOURCE_ID = 'settlement-overview-image';
const TRANSPARENT_PIXEL_DATA_URL =
  'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=';
const SINGLE_LAYER_OPACITY = 0.82;
const WEATHER_LAYER_IDS = new Set(['temperature']);

function isTemperaturePriorityMode(preferences: Record<string, LayerPreference>): boolean {
  const temperaturePref = preferences['temperature'];
  return !!temperaturePref?.enabled && temperaturePref.importance > 0;
}
@Injectable({
  providedIn: 'root',
})
export class ZarrMapService {
  private readonly translate = inject(TranslateService);
  private map: MaplibreMap | null = null;
  private readonly managedLayers = new Map<string, ManagedZarrLayer>();
  private sampleGeneration = 0;
  private sampleAbort: AbortController | null = null;
  private compositeAbort: AbortController | null = null;
  private compositeDebounce: ReturnType<typeof setTimeout> | null = null;
  private rescoreDebounce: ReturnType<typeof setTimeout> | null = null;
  private lastSample: { lng: number; lat: number } | null = null;
  private overviewDataUrl: string | null = null;
  private readonly rawCache = new OverviewRawCache(32);
  private lastExtentKey: string | null = null;
  private lastLodTier: string | null = null;
  private lastRawByLayerId = new Map<string, Map<string, number>>();
  private lastCompositeFingerprint: string | null = null;
  private overviewGeneration = 0;
  private lastFetchLayerCount = 0;
  private compositeRunId = 0;
  private pendingCompositeExtentKey: string | null = null;
  private deferredLayerReadyRescore = false;
  private lastOverviewFootprintSpan = 0;
  private lastOverviewFullCells = 0;
  private pendingOverviewFullCells = 0;
  private weatherCacheToken: string | null = null;
  private readonly onMapViewChange = (): void => this.scheduleOverviewComposite();

  readonly layerPreferences = signal<Record<string, LayerPreference>>(
    createInitialLayerPreferences(),
  );
  readonly layerMeta = signal<Record<string, SettlementLayerMeta | null>>({});
  readonly metaFallback = signal<Record<string, boolean>>({});
  readonly metrics = signal<LocationMetrics>({ ...EMPTY_LOCATION_METRICS });
  readonly metricsLoading = signal(false);
  readonly metricsError = signal<string | null>(null);
  readonly layerStates = signal<ZarrLayerState[]>([]);
  readonly overviewOpacity = signal<number>(70);
  readonly overviewLoading = signal(false);
  readonly overviewCacheStats = signal(this.rawCache.stats());
  readonly overviewScore = computed(() =>
    computePreferenceOverview(this.metrics(), {
      definitions: ZARR_LAYER_DEFINITIONS,
      preferences: this.layerPreferences(),
      metaByLayerId: this.layerMeta(),
    }),
  );

  attachToMap(map: MaplibreMap): void {
    if (this.map === map && this.managedLayers.size > 0) {
      return;
    }

    this.detachFromMap();
    this.map = map;

    for (const definition of ZARR_LAYER_DEFINITIONS) {
      const layer = this.createZarrLayer(definition, this.cacheTokenForLayer(definition.id));

      this.managedLayers.set(definition.id, {
        definition,
        layer,
        ready: false,
        loading: true,
      });

      void this.fetchLayerMeta(definition, this.cacheTokenForLayer(definition.id));
    }

    this.syncLayerStateSignal();
    this.installLayersOnMap(map);
    exposeOverviewForE2e(
      () => this.getOverviewE2eState(),
      () => this.syncOverviewToViewport(),
    );
    exposeZarrSampleForE2e((lng, lat, layerId) => this.sampleLayerAt(lng, lat, layerId));
  }

  refreshMeteoLayers(manifest: MeteoManifest, reloadRaster = true): void {
    this.weatherCacheToken = manifest.last_updated;

    if (!reloadRaster || !this.map) {
      return;
    }

    for (const weatherLayerId of WEATHER_LAYER_IDS) {
      const managed = this.managedLayers.get(weatherLayerId);
      if (!managed) {
        continue;
      }

      if (this.map.getLayer(weatherLayerId)) {
        this.map.removeLayer(weatherLayerId);
      }

      const layer = this.createZarrLayer(
        managed.definition,
        this.cacheTokenForLayer(weatherLayerId),
      );
      managed.layer = layer;
      managed.loading = true;
      managed.ready = false;

      this.map.addLayer(
        layer as unknown as CustomLayerInterface,
        this.map.getLayer(OVERVIEW_MAP_LAYER_ID) ? OVERVIEW_MAP_LAYER_ID : undefined,
      );
      void this.fetchLayerMeta(managed.definition, this.cacheTokenForLayer(weatherLayerId));
    }

    this.syncLayerStateSignal();
    this.applyLayerDisplay();
  }

  /** Point sample for e2e / debugging (WGS84). */
  async sampleLayerAt(lng: number, lat: number, layerId: string): Promise<number | null> {
    const managed = this.managedLayers.get(layerId);
    if (!managed?.ready) {
      return null;
    }
    const point = { type: 'Point' as const, coordinates: [lng, lat] as [number, number] };
    try {
      const result = await managed.layer.queryData(point, managed.definition.selector);
      return extractScalar(result, managed.definition.variable);
    } catch {
      return null;
    }
  }

  /** Rebuild the overview raster for the current map viewport (skips debounce). */
  syncOverviewToViewport(): void {
    if (this.compositeDebounce) {
      clearTimeout(this.compositeDebounce);
      this.compositeDebounce = null;
    }
    void this.runOverviewComposite();
  }

  detachFromMap(): void {
    if (this.map) {
      this.map.off('moveend', this.onMapViewChange);
      this.map.off('zoomend', this.onMapViewChange);
      for (const { layer } of this.managedLayers.values()) {
        if (this.map.getLayer(layer.id)) {
          this.map.removeLayer(layer.id);
        }
      }
      if (this.map.getLayer(OVERVIEW_MAP_LAYER_ID)) {
        this.map.removeLayer(OVERVIEW_MAP_LAYER_ID);
      }
      if (this.map.getSource(OVERVIEW_SOURCE_ID)) {
        this.map.removeSource(OVERVIEW_SOURCE_ID);
      }
    }
    if (this.overviewDataUrl) {
      URL.revokeObjectURL(this.overviewDataUrl);
      this.overviewDataUrl = null;
    }
    this.rawCache.clear();
    this.lastRawByLayerId.clear();
    this.lastExtentKey = null;
    this.managedLayers.clear();
    this.map = null;
    this.syncLayerStateSignal();
  }

  setLayerPreference(layerId: string, preference: LayerPreference): void {
    this.layerPreferences.update((prev) => ({
      ...prev,
      [layerId]: clampLayerPreference(preference),
    }));
    this.applyLayerDisplay({ rescoreOnly: true });
    this.syncLayerStateSignal();
  }

  resetLayerPreference(layerId: string): void {
    const defaults = resetPreferencesForActivePreset();
    if (defaults[layerId]) {
      this.setLayerPreference(layerId, defaults[layerId]);
    }
  }

  resetAllPreferences(): void {
    this.layerPreferences.set(resetPreferencesForActivePreset());
    this.applyLayerDisplay({ rescoreOnly: true });
    this.syncLayerStateSignal();
  }

  applyLifestylePreset(presetId: LifestylePresetId): void {
    storeLifestylePresetId(presetId);
    this.layerPreferences.set(createPreferencesForPreset(presetId));
    this.applyLayerDisplay({ rescoreOnly: true });
    this.syncLayerStateSignal();
  }

  setLayerEnabled(layerId: string, enabled: boolean): void {
    if (!this.managedLayers.has(layerId)) {
      return;
    }
    const current = this.layerPreferences()[layerId];
    if (!current) {
      return;
    }
    this.setLayerPreference(layerId, { ...current, enabled });
  }

  setAllLayersEnabled(enabled: boolean): void {
    this.layerPreferences.update((prev) => {
      const next = { ...prev };
      for (const id of Object.keys(next)) {
        next[id] = { ...next[id], enabled };
      }
      return next;
    });
    this.applyLayerDisplay({ rescoreOnly: !enabled });
    this.syncLayerStateSignal();
  }

  /** @deprecated Use setLayerPreference */
  setLayerWeight(layerId: string, weight: number): void {
    const current = this.layerPreferences()[layerId];
    if (!current) {
      return;
    }
    this.setLayerPreference(layerId, { ...current, importance: weight });
  }

  /** Sample all layers at a point without updating global metrics state. */
  async queryMetricsAt(lng: number, lat: number, signal?: AbortSignal): Promise<LocationMetrics> {
    const point = { type: 'Point' as const, coordinates: [lng, lat] as [number, number] };
    const next: LocationMetrics = { ...EMPTY_LOCATION_METRICS };

    await Promise.all(
      [...this.managedLayers.values()].map(async ({ definition, layer, ready }) => {
        if (!ready) {
          return;
        }

        const result = await layer.queryData(point, definition.selector, { signal });
        const value = extractScalar(result, definition.variable);
        next[definition.metricKey] = value;
      }),
    );

    return next;
  }

  setMetrics(metrics: LocationMetrics): void {
    this.metrics.set(metrics);
  }

  async sampleLocation(lng: number, lat: number): Promise<void> {
    this.lastSample = { lng, lat };
    const generation = ++this.sampleGeneration;
    this.sampleAbort?.abort();
    this.sampleAbort = new AbortController();
    const { signal } = this.sampleAbort;

    this.metricsLoading.set(true);
    this.metricsError.set(null);

    try {
      const next = await this.queryMetricsAt(lng, lat, signal);

      if (generation === this.sampleGeneration) {
        this.metrics.set(next);
      }
    } catch (err) {
      if (generation !== this.sampleGeneration || signal.aborted) {
        return;
      }
      const message =
        err instanceof Error ? err.message : this.translate.instant('errors.zarrQueryFailed');
      this.metricsError.set(message);
      console.error('[zarr] sampleLocation', err);
    } finally {
      if (generation === this.sampleGeneration) {
        this.metricsLoading.set(false);
      }
    }
  }

  private async fetchLayerMeta(
    definition: ZarrLayerDefinition,
    cacheToken?: string | null,
  ): Promise<void> {
    if (!environment.settlementLayerMetaAvailable) {
      this.layerMeta.update((prev) => ({ ...prev, [definition.id]: null }));
      this.metaFallback.update((prev) => ({ ...prev, [definition.id]: true }));
      this.syncLayerStateSignal();
      if (this.overviewLoading()) {
        this.deferredLayerReadyRescore = true;
      } else {
        this.scheduleOverviewRescore();
      }
      return;
    }

    const url = addCacheToken(settlementLayerMetaUrl(definition.storePath), cacheToken);
    try {
      const res = await fetch(url);
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const meta = (await res.json()) as SettlementLayerMeta;
      this.layerMeta.update((prev) => ({ ...prev, [definition.id]: meta }));
      this.metaFallback.update((prev) => ({ ...prev, [definition.id]: false }));
      const managed = this.managedLayers.get(definition.id);
      if (managed?.ready && definition.id !== 'temperature') {
        managed.layer.setClim([meta.p5, meta.p95]);
      }
    } catch {
      console.warn(`[zarr] meta fallback for ${definition.id} (using clim)`);
      this.layerMeta.update((prev) => ({ ...prev, [definition.id]: null }));
      this.metaFallback.update((prev) => ({ ...prev, [definition.id]: true }));
    }
    this.syncLayerStateSignal();
    if (this.overviewLoading()) {
      this.deferredLayerReadyRescore = true;
    } else {
      this.scheduleOverviewRescore();
    }
  }

  private installLayersOnMap(map: MaplibreMap): void {
    const addLayers = () => {
      for (const { layer } of this.managedLayers.values()) {
        if (!map.getLayer(layer.id)) {
          map.addLayer(layer as unknown as CustomLayerInterface);
        }
      }
      if (!map.getSource(OVERVIEW_SOURCE_ID)) {
        map.addSource(OVERVIEW_SOURCE_ID, {
          type: 'image',
          url: TRANSPARENT_PIXEL_DATA_URL,
          coordinates: [
            [8.2, 47.5],
            [8.8, 47.5],
            [8.8, 47.0],
            [8.2, 47.0],
          ],
        });
      }
      if (!map.getLayer(OVERVIEW_MAP_LAYER_ID)) {
        map.addLayer({
          id: OVERVIEW_MAP_LAYER_ID,
          type: 'raster',
          source: OVERVIEW_SOURCE_ID,
          paint: {
            'raster-opacity': this.overviewOpacity() / 100,
            'raster-resampling': 'linear',
            'raster-fade-duration': 0,
          },
        });
      }
      map.on('moveend', this.onMapViewChange);
      map.on('zoomend', this.onMapViewChange);
      this.applyLayerDisplay();
    };

    if (map.loaded()) {
      addLayers();
    } else {
      map.once('load', addLayers);
    }
  }

  private createZarrLayer(definition: ZarrLayerDefinition, cacheToken?: string | null): ZarrLayer {
    const layerOptions: ConstructorParameters<typeof ZarrLayer>[0] = {
      id: definition.id,
      source: addCacheToken(definition.storePath, cacheToken),
      variable: definition.variable,
      selector: definition.selector,
      bounds: definition.bounds,
      fillValue: definition.fillValue,
      colormap: definition.colormap,
      clim: definition.clim,
      opacity: 0,
      zarrVersion: 3,
      proj4: SWISS_LV95_PROJ4,
      spatialDimensions: { lat: 'y', lon: 'x' },
      latIsAscending: definition.latIsAscending,
      onLoadingStateChange: (state) => {
        this.updateLayerState(definition.id, {
          loading: state.loading || state.metadata,
          ready: !state.loading && !state.metadata && !state.error,
        });
        if (state.error) {
          console.error(`[zarr] ${definition.id}`, state.error);
        }
      },
    };

    if (definition.fillValue !== undefined) {
      layerOptions.fillValue = definition.fillValue;
    } else if (ZARR_LAYERS_WITH_NAN_FILL.has(definition.id)) {
      layerOptions.fillValue = Number.NaN;
    }

    return new ZarrLayer(layerOptions);
  }

  private cacheTokenForLayer(layerId: string): string | null {
    if (!WEATHER_LAYER_IDS.has(layerId)) {
      return null;
    }
    return this.weatherCacheToken;
  }

  private applyLayerDisplay(options?: { rescoreOnly?: boolean }): void {
    if (!this.map) {
      return;
    }

    const preferences = this.layerPreferences();

    if (isTemperaturePriorityMode(preferences)) {
      this.compositeAbort?.abort();
      if (this.compositeDebounce) {
        clearTimeout(this.compositeDebounce);
        this.compositeDebounce = null;
      }
      if (this.rescoreDebounce) {
        clearTimeout(this.rescoreDebounce);
        this.rescoreDebounce = null;
      }
      this.pendingCompositeExtentKey = null;
      this.pendingOverviewFullCells = 0;
      this.overviewLoading.set(false);

      for (const { definition, layer, ready } of this.managedLayers.values()) {
        if (!this.map.getLayer(layer.id)) {
          continue;
        }
        const preference = preferences[definition.id];
        const baseOpacity = definition.renderOpacity ?? SINGLE_LAYER_OPACITY;
        const showTemperature = definition.id === 'temperature' && ready;
        layer.setOpacity(showTemperature ? singleLayerOpacityFromImportance(baseOpacity, preference) : 0);
      }

      if (this.map.getLayer(OVERVIEW_MAP_LAYER_ID)) {
        this.map.setLayoutProperty(OVERVIEW_MAP_LAYER_ID, 'visibility', 'none');
        this.map.setPaintProperty(OVERVIEW_MAP_LAYER_ID, 'raster-opacity', 0);
      }

      const overviewSource = this.map.getSource(OVERVIEW_SOURCE_ID) as ImageSource | undefined;
      const extent = overviewExtentForMap(this.map);
      if (overviewSource && extent) {
        overviewSource.updateImage({
          url: TRANSPARENT_PIXEL_DATA_URL,
          coordinates: cellExtentToImageCoordinates(extent),
        });
      }

      this.map.triggerRepaint();
      return;
    }

    const displayPlan = computeLayerDisplayPlan(
      preferences,
      [...this.managedLayers.values()].map((managed) => ({
        id: managed.definition.id,
        ready: managed.ready,
        includeInOverview: managed.definition.includeInOverview !== false,
      })),
    );

    for (const { definition, layer, ready } of this.managedLayers.values()) {
      if (!this.map.getLayer(layer.id)) {
        continue;
      }
      const preference = preferences[definition.id];
      const showSingle = displayPlan.visibleLayerIds.includes(definition.id);
      const baseOpacity = definition.renderOpacity ?? SINGLE_LAYER_OPACITY;
      const layerOpacity = showSingle
        ? singleLayerOpacityFromSlider(baseOpacity, preference, this.overviewOpacity() / 100)
        : 0;
      layer.setOpacity(layerOpacity);
    }

    if (this.map.getLayer(OVERVIEW_MAP_LAYER_ID)) {
      this.map.setLayoutProperty(
        OVERVIEW_MAP_LAYER_ID,
        'visibility',
        displayPlan.hasOverview ? 'visible' : 'none',
      );
      this.map.setPaintProperty(
        OVERVIEW_MAP_LAYER_ID,
        'raster-opacity',
        displayPlan.hasOverview ? this.overviewOpacity() / 100 : 0,
      );
    }

    if (options?.rescoreOnly) {
      this.scheduleOverviewRescore();
    } else {
      this.scheduleOverviewComposite();
    }
    this.map.triggerRepaint();
  }

  setOverviewOpacity(value: number): void {
    this.overviewOpacity.set(Math.min(100, Math.max(0, Math.round(value))));
    this.applyLayerDisplay({ rescoreOnly: true });
  }

  private scheduleOverviewRescore(): void {
    if (this.rescoreDebounce) {
      clearTimeout(this.rescoreDebounce);
    }
    this.rescoreDebounce = setTimeout(() => void this.runOverviewRescore(), 50);
  }

  private scheduleOverviewComposite(): void {
    if (this.compositeDebounce) {
      clearTimeout(this.compositeDebounce);
    }

    if (this.map && this.overviewLoading()) {
      const requested = overviewExtentForMap(this.map);
      if (requested && this.pendingOverviewFullCells > requested.fullNx * requested.fullNy * 4) {
        return;
      }
    }

    let delayMs = 300;
    if (this.map) {
      const extent = overviewExtentForMap(this.map);
      if (extent) {
        const plan = resolveOverviewLod(this.map.getZoom(), extent.fullNx, extent.fullNy);
        delayMs = overviewCompositeDebounceMs(plan);
      }
    }
    this.compositeDebounce = setTimeout(() => void this.runOverviewComposite(), delayMs);
  }

  private buildSources(plan: ReturnType<typeof resolveOverviewLod>) {
    return [...this.managedLayers.values()]
      .filter(({ definition }) => isOverviewEligible(definition.includeInOverview))
      .map(({ definition, layer, ready }) => ({
        definition,
        ready,
        queryContext: { definition, layer, plan },
      }));
  }

  private async runOverviewRescore(): Promise<void> {
    if (!this.map || this.lastExtentKey === null || this.lastRawByLayerId.size === 0) {
      if (this.overviewLoading()) {
        return;
      }
      await this.runOverviewComposite();
      return;
    }

    const extent = overviewExtentForMap(this.map);
    if (!extent || extentCacheKey(extent) !== this.lastExtentKey) {
      if (this.overviewLoading()) {
        return;
      }
      await this.runOverviewComposite();
      return;
    }

    if (this.overviewLoading()) {
      return;
    }

    const preferences = this.layerPreferences();
    const activeIds = [...this.managedLayers.keys()].filter((id) => {
      const p = preferences[id];
      return p?.enabled && p.importance > 0;
    });
    const fingerprint = `${this.lastExtentKey}|${preferencesFingerprint(preferences, activeIds)}`;
    if (fingerprint === this.lastCompositeFingerprint) {
      return;
    }

    const plan = resolveOverviewLod(this.map.getZoom(), extent.fullNx, extent.fullNy);
    const queryBounds = overviewQueryBoundsForExtent(extent);
    const sources = this.buildSources(plan);

    const missing = sources.filter((s) => {
      const pref = preferences[s.definition.id];
      return (
        s.ready &&
        pref?.enabled &&
        pref.importance > 0 &&
        !this.lastRawByLayerId.has(s.definition.id)
      );
    });

    if (missing.length > 0) {
      this.compositeAbort?.abort();
      this.compositeAbort = new AbortController();
      const { signal } = this.compositeAbort;
      this.lastFetchLayerCount = 0;
      const fetched = await fetchOverviewRawMaps(
        {
          sources: missing,
          preferences,
          metaByLayerId: this.layerMeta(),
          west: queryBounds.west,
          south: queryBounds.south,
          east: queryBounds.east,
          north: queryBounds.north,
          zoom: this.map.getZoom(),
          plan,
          rawCache: this.rawCache,
          signal,
          onFetchLayer: () => {
            this.lastFetchLayerCount += 1;
          },
        },
        extent,
      );
      if (signal.aborted) {
        return;
      }
      for (const [id, map] of fetched) {
        this.lastRawByLayerId.set(id, map);
      }
    }

    const composite = scoreOverviewComposite({
      rawByLayerId: this.lastRawByLayerId,
      sources,
      preferences,
      metaByLayerId: this.layerMeta(),
      extent,
    });

    this.overviewGeneration += 1;
    this.lastCompositeFingerprint = fingerprint;
    this.overviewCacheStats.set(this.rawCache.stats());
    await this.applyCompositeToMap(composite, extent);
  }

  private async runOverviewComposite(): Promise<void> {
    if (!this.map) {
      return;
    }

    const zoom = this.map.getZoom();

    const extent = overviewExtentForMap(this.map);
    if (!extent) {
      return;
    }
    const queryBounds = overviewQueryBoundsForExtent(extent);

    this.pendingOverviewFullCells = extent.fullNx * extent.fullNy;

    const extentKey = extentCacheKey(extent);
    const inFlightForExtent =
      this.overviewLoading() &&
      this.pendingCompositeExtentKey === extentKey &&
      this.compositeAbort &&
      !this.compositeAbort.signal.aborted;

    if (inFlightForExtent) {
      return;
    }

    if (this.pendingCompositeExtentKey !== null && this.pendingCompositeExtentKey !== extentKey) {
      this.compositeAbort?.abort();
    }
    if (!this.compositeAbort || this.compositeAbort.signal.aborted) {
      this.compositeAbort = new AbortController();
    }
    const { signal } = this.compositeAbort;
    const runId = ++this.compositeRunId;
    this.pendingCompositeExtentKey = extentKey;

    const plan = resolveOverviewLod(zoom, extent.fullNx, extent.fullNy);
    const tierChanged = this.lastLodTier !== null && this.lastLodTier !== plan.tier;
    if (tierChanged) {
      this.rawCache.clear();
    }
    this.lastLodTier = plan.tier;

    this.overviewLoading.set(true);
    this.lastFetchLayerCount = 0;

    const sources = this.buildSources(plan);
    const preferences = this.layerPreferences();

    try {
      const rawByLayerId = await fetchOverviewRawMaps(
        {
          sources,
          preferences,
          metaByLayerId: this.layerMeta(),
          west: queryBounds.west,
          south: queryBounds.south,
          east: queryBounds.east,
          north: queryBounds.north,
          zoom,
          plan,
          rawCache: this.rawCache,
          signal,
          onFetchLayer: () => {
            this.lastFetchLayerCount += 1;
          },
        },
        extent,
      );

      if (signal.aborted || !this.map) {
        return;
      }

      this.lastRawByLayerId = rawByLayerId;
      this.lastExtentKey = extentCacheKey(extent);

      const activeIds = [...this.managedLayers.keys()].filter((id) => {
        const p = preferences[id];
        return p?.enabled && p.importance > 0;
      });
      this.lastCompositeFingerprint = `${this.lastExtentKey}|${preferencesFingerprint(preferences, activeIds)}`;

      const composite = scoreOverviewComposite({
        rawByLayerId,
        sources,
        preferences,
        metaByLayerId: this.layerMeta(),
        extent,
      });

      this.overviewGeneration += 1;
      this.overviewCacheStats.set(this.rawCache.stats());
      await this.applyCompositeToMap(composite ?? null, extent);

      if (this.deferredLayerReadyRescore) {
        this.deferredLayerReadyRescore = false;
        void this.runOverviewRescore();
      }
    } catch (err) {
      if (!signal.aborted) {
        console.error('[overview] composite build failed', err);
        await this.applyCompositeToMap(null, extent);
      }
    } finally {
      if (runId === this.compositeRunId) {
        this.overviewLoading.set(false);
        if (this.pendingCompositeExtentKey === extentKey) {
          this.pendingCompositeExtentKey = null;
          this.pendingOverviewFullCells = 0;
        }
      }
    }
  }

  private async applyCompositeToMap(
    composite: OverviewCompositeResult | null,
    extent?: ViewportCellExtent,
  ): Promise<void> {
    if (!this.map) {
      return;
    }

    const source = this.map.getSource(OVERVIEW_SOURCE_ID) as ImageSource | undefined;
    if (!source) {
      return;
    }

    const coordinates = composite
      ? composite.coordinates
      : extent
        ? cellExtentToImageCoordinates(extent)
        : null;

    if (!coordinates) {
      return;
    }

    this.lastOverviewFootprintSpan = ZarrMapService.footprintSpan(coordinates);
    if (extent) {
      this.lastOverviewFullCells = extent.fullNx * extent.fullNy;
    }

    if (!composite) {
      source.updateImage({
        url:
          this.overviewDataUrl ?? TRANSPARENT_PIXEL_DATA_URL,
        coordinates,
      });
      this.map.triggerRepaint();
      return;
    }

    if (this.overviewDataUrl) {
      URL.revokeObjectURL(this.overviewDataUrl);
    }
    this.overviewDataUrl = composite.canvas.toDataURL('image/png');
    source.updateImage({
      url: this.overviewDataUrl,
      coordinates,
    });
    this.map.triggerRepaint();
  }

  private getOverviewE2eState(): OverviewE2eState {
    return {
      generation: this.overviewGeneration,
      lastFetchLayerCount: this.lastFetchLayerCount,
      cacheHits: this.rawCache.stats().hits,
      cacheMisses: this.rawCache.stats().misses,
      loading: this.overviewLoading(),
      imageFootprintSpan: this.lastOverviewFootprintSpan,
      overviewFullCells: this.lastOverviewFullCells,
    };
  }

  private static footprintSpan(
    coordinates: [[number, number], [number, number], [number, number], [number, number]],
  ): number {
    const lngs = coordinates.map((c) => c[0]);
    const lats = coordinates.map((c) => c[1]);
    return Math.max(...lngs) - Math.min(...lngs) + (Math.max(...lats) - Math.min(...lats));
  }

  private updateLayerState(
    id: string,
    patch: Partial<Pick<ManagedZarrLayer, 'ready' | 'loading'>>,
  ): void {
    const managed = this.managedLayers.get(id);
    if (!managed) {
      return;
    }

    if (patch.loading !== undefined) {
      managed.loading = patch.loading;
    }

    if (patch.ready !== undefined) {
      const becameReady = patch.ready && !managed.ready;
      managed.ready = patch.ready;
      if (becameReady && this.lastSample) {
        void this.sampleLocation(this.lastSample.lng, this.lastSample.lat);
      }
      if (patch.ready) {
        if (this.overviewLoading()) {
          this.deferredLayerReadyRescore = true;
        } else if (this.lastExtentKey !== null) {
          this.applyLayerDisplay({ rescoreOnly: true });
        } else {
          this.applyLayerDisplay();
        }
        this.map?.triggerRepaint();
      }
    }

    this.syncLayerStateSignal();
  }

  private syncLayerStateSignal(): void {
    const preferences = this.layerPreferences();
    const meta = this.layerMeta();
    const fallback = this.metaFallback();
    this.layerStates.set(
      [...this.managedLayers.values()].map(({ definition, ready, loading }) => ({
        id: definition.id,
        labelKey: definition.labelKey,
        descriptionKey: definition.descriptionKey,
        colormap: definition.colormap,
        clim: definition.clim,
        enabled: preferences[definition.id]?.enabled !== false,
        preference: preferences[definition.id] ?? createInitialLayerPreferences()[definition.id],
        meta: meta[definition.id] ?? null,
        metaFromFallback: fallback[definition.id] ?? true,
        ready,
        loading,
      })),
    );
  }
}

function extractScalar(result: QueryResult, variable: string): number | null {
  const raw = result[variable];
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    return raw;
  }
  if (typeof raw === 'bigint') {
    return Number(raw);
  }
  if (Array.isArray(raw) || ArrayBuffer.isView(raw)) {
    const arr = raw as unknown as ArrayLike<unknown>;
    if (arr.length > 0) {
      const value = arr[0];
      if (typeof value === 'number' && Number.isFinite(value)) {
        return value;
      }
      if (typeof value === 'bigint') {
        return Number(value);
      }
    }
  }
  return null;
}

function addCacheToken(url: string, cacheToken?: string | null): string {
  if (!cacheToken) {
    return url;
  }
  const separator = url.includes('?') ? '&' : '?';
  return `${url}${separator}v=${encodeURIComponent(cacheToken)}`;
}
