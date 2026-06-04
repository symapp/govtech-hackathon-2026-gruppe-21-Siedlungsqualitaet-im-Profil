import { Component, OnInit, OnDestroy, ElementRef, ViewChild, effect, inject } from '@angular/core';
import { TranslateService } from '@ngx-translate/core';
import { LocationService, type RegionOfInterest } from '../../services/location.service';
import { getAmenityIcon, type NearbyAmenity } from '../../services/overpass.service';
import { ZarrMapService } from '../../services/zarr-map.service';
import { GeocodingService } from '../../services/geocoding.service';
import { exposeMapForE2e } from '../../testing/e2e-map.harness';
import { Map, NavigationControl, Marker, Popup, type MapMouseEvent } from 'maplibre-gl';
import { MapboxOverlay } from '@deck.gl/mapbox';
import { PolygonLayer } from '@deck.gl/layers';
import {
  amenityMarkerDisplayForZoom,
  createAmenityMarkerElement,
  setAmenityMarkerDisplay,
} from '../../utils/amenity-map-pin.util';

import { clampToSwitzerland, SWITZERLAND_MAX_BOUNDS } from '../../config/map-bounds.config';
import { mapUiPaddingEquals, readMapUiPadding } from '../../utils/map-ui-insets.util';
import type { PaddingOptions } from 'maplibre-gl';

@Component({
  selector: 'app-map',
  standalone: true,
  templateUrl: './map.component.html',
  styleUrl: './map.component.scss',
})
export class MapComponent implements OnInit, OnDestroy {
  @ViewChild('mapContainer', { static: true }) mapContainer!: ElementRef<HTMLDivElement>;

  private map!: Map;
  private marker!: Marker;
  private amenityMarkerEntries: Array<{
    marker: Marker;
    element: HTMLDivElement;
    amenity: NearbyAmenity;
  }> = [];
  private amenityHoverPopup: Popup | null = null;
  private amenityHoverPopupAmenityId: string | null = null;
  private amenityPopupHideTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly amenityPopupOffsetDot: [number, number] = [0, -15];
  private readonly amenityPopupOffsetPin: [number, number] = [0, -40];
  private readonly amenityPopupHideDelayMs = 180;
  private readonly onMapZoomChange = (): void => {
    this.applyAmenityMarkerZoomMode();
  };
  private deckOverlay!: MapboxOverlay;
  private locationService = inject(LocationService);
  private zarrMapService = inject(ZarrMapService);
  private geocodingService = inject(GeocodingService);
  private readonly translate = inject(TranslateService);
  private lastFlyToRegionKey: string | null = null;
  private reverseAbort: AbortController | null = null;
  private lastUiPadding: PaddingOptions = { top: 0, bottom: 0, left: 0, right: 0 };
  private uiPaddingCleanup: (() => void) | null = null;
  private readonly onUiPaddingChange = (): void => {
    this.applyUiPadding();
  };

  constructor() {
    effect(() => {
      const regions = this.locationService.regions();
      const activeRegion = this.locationService.activeRegion();
      const amenitiesEnabled = this.locationService.amenitiesEnabled();
      const amenities = amenitiesEnabled ? this.locationService.allAmenities() : [];

      if (this.map && this.marker && this.deckOverlay) {
        this.updateDeckLayers(regions, activeRegion?.id ?? '');
        this.syncAmenityMarkers(amenities);

        if (!activeRegion) {
          this.marker.getElement().style.display = 'none';
          return;
        }

        this.marker.getElement().style.display = '';
        this.marker.setLngLat([activeRegion.lng, activeRegion.lat]);

        const flyKey = `${activeRegion.id}:${activeRegion.lng.toFixed(5)},${activeRegion.lat.toFixed(5)}`;
        if (this.lastFlyToRegionKey !== flyKey && this.map.getZoom() >= 10) {
          this.lastFlyToRegionKey = flyKey;
          this.map.flyTo({ center: [activeRegion.lng, activeRegion.lat], essential: true });
        } else if (this.lastFlyToRegionKey !== flyKey) {
          this.lastFlyToRegionKey = flyKey;
        }
      }
    });
  }

  ngOnInit(): void {
    this.initMap();
    const activeRegion = this.locationService.activeRegion();
    if (activeRegion) {
      void this.applyReverseAutoName(activeRegion.lat, activeRegion.lng);
    }
  }

  ngOnDestroy(): void {
    this.reverseAbort?.abort();
    this.clearAmenityMarkers();
    this.uiPaddingCleanup?.();
    this.uiPaddingCleanup = null;
    this.zarrMapService.detachFromMap();
    if (this.deckOverlay) {
      this.deckOverlay.finalize();
    }
    if (this.map) {
      this.map.off('zoom', this.onMapZoomChange);
      this.map.off('zoomend', this.onMapZoomChange);
      this.map.remove();
    }
  }

  private initMap(): void {
    const activeRegion = this.locationService.activeRegion();
    const lat = activeRegion?.lat ?? 46.99718;
    const lng = activeRegion?.lng ?? 7.46274;
    this.locationService.setViewCenter(lat, lng);

    const mapOptions = {
      container: this.mapContainer.nativeElement,
      style: 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json',
      center: [lng, lat],
      zoom: 14,
      pitch: 0,
      maxPitch: 0,
      dragRotate: false,
      touchPitch: false,
      pitchWithRotate: false,
      maxBounds: SWITZERLAND_MAX_BOUNDS,
      antialias: true,
      canvasContextAttributes: { preserveDrawingBuffer: true },
    } as any;

    this.map = new Map(mapOptions);

    this.map.addControl(
      new NavigationControl({ showCompass: true, showZoom: true, visualizePitch: false }),
      'top-left',
    );

    this.marker = new Marker({
      draggable: true,
      color: '#6366f1',
      className: 'region-map-marker',
    })
      .setLngLat([lng, lat])
      .addTo(this.map);

    this.marker.on('drag', () => {
      const lngLat = this.marker.getLngLat();
      const clamped = clampToSwitzerland(lngLat.lng, lngLat.lat);
      if (clamped.lng !== lngLat.lng || clamped.lat !== lngLat.lat) {
        this.marker.setLngLat([clamped.lng, clamped.lat]);
      }
    });

    this.marker.on('dragend', () => {
      void this.handleMarkerDragEnd();
    });

    this.map.on('click', (event: MapMouseEvent) => {
      if (!this.locationService.activeRegion()) {
        return;
      }
      void this.moveActiveRegionTo(event.lngLat.lng, event.lngLat.lat);
    });

    this.deckOverlay = new MapboxOverlay({
      // Keep overlay in its own canvas so region circles/points stay visible above rasters.
      interleaved: false,
      layers: [],
    });

    this.map.addControl(this.deckOverlay as any);
    this.repositionDeckOverlayLayer();
    exposeMapForE2e(this.map);
    this.zarrMapService.attachToMap(this.map);

    this.map.on('movestart', () => {
      this.hideAmenityPopupImmediate();
    });

    this.map.on('zoom', this.onMapZoomChange);
    this.map.on('zoomend', this.onMapZoomChange);

    this.map.on('moveend', () => {
      const center = this.map.getCenter();
      this.locationService.setViewCenter(center.lat, center.lng);
    });

    this.map.on('load', () => {
      this.map.setPitch(0);
      this.map.setProjection({ type: 'mercator' });
      this.repositionDeckOverlayLayer();
      this.applyUiPadding();
      this.setupUiPaddingSync();
      const regions = this.locationService.regions();
      const currentActiveRegion = this.locationService.activeRegion();
      const amenitiesEnabled = this.locationService.amenitiesEnabled();
      const amenities = amenitiesEnabled ? this.locationService.allAmenities() : [];
      this.updateDeckLayers(regions, currentActiveRegion?.id ?? '');
      this.syncAmenityMarkers(amenities);
      const center = this.map.getCenter();
      this.locationService.setViewCenter(center.lat, center.lng);
    });
  }

  /**
   * Deck.gl is added as a control (full-map overlay). Move it into the canvas stack
   * between the basemap and MapLibre markers so pins stay visible above radius circles.
   */
  private repositionDeckOverlayLayer(): void {
    if (!this.map) {
      return;
    }

    const canvasContainer = this.map.getContainer().querySelector('.maplibregl-canvas-container');
    if (!canvasContainer) {
      return;
    }

    const deckContainer =
      this.map.getContainer().querySelector('.deck-widget-container') ??
      this.map
        .getContainer()
        .querySelector('.maplibregl-ctrl-top-left > div:not(.maplibregl-ctrl-group)');

    if (
      !(deckContainer instanceof HTMLElement) ||
      deckContainer.parentElement === canvasContainer
    ) {
      return;
    }

    const firstMarker = canvasContainer.querySelector('.maplibregl-marker');
    if (firstMarker) {
      canvasContainer.insertBefore(deckContainer, firstMarker);
    } else {
      canvasContainer.appendChild(deckContainer);
    }

    deckContainer.style.zIndex = '1';
    deckContainer.style.pointerEvents = 'none';
  }

  private applyUiPadding(): void {
    if (!this.map) {
      return;
    }
    const padding = readMapUiPadding();
    if (mapUiPaddingEquals(padding, this.lastUiPadding)) {
      return;
    }
    this.lastUiPadding = padding;
    this.map.setPadding(padding);
    this.map.resize();
  }

  private setupUiPaddingSync(): void {
    this.uiPaddingCleanup?.();

    const observed = new Set<Element>();
    const resizeObserver = new ResizeObserver(() => this.applyUiPadding());
    const observe = (selector: string): void => {
      const element = document.querySelector(selector);
      if (element && !observed.has(element)) {
        observed.add(element);
        resizeObserver.observe(element);
      }
    };

    for (const selector of [
      '.regions-sidebar',
      '.sidebar',
      '.search-container',
      '.mobile-map-toolbar',
    ]) {
      observe(selector);
    }
    resizeObserver.observe(document.body);

    const mutationObserver = new MutationObserver(() => {
      for (const selector of [
        '.regions-sidebar',
        '.sidebar',
        '.search-container',
        '.mobile-map-toolbar',
      ]) {
        observe(selector);
      }
      this.applyUiPadding();
    });
    for (const selector of ['.regions-sidebar', '.sidebar']) {
      const element = document.querySelector(selector);
      if (element) {
        mutationObserver.observe(element, { attributes: true, attributeFilter: ['class'] });
      }
    }
    const mainLayout = document.querySelector('.main-layout');
    if (mainLayout) {
      mutationObserver.observe(mainLayout, { childList: true });
    }

    window.addEventListener('resize', this.onUiPaddingChange);
    this.uiPaddingCleanup = () => {
      resizeObserver.disconnect();
      mutationObserver.disconnect();
      window.removeEventListener('resize', this.onUiPaddingChange);
    };
  }

  private syncAmenityMarkers(amenities: NearbyAmenity[]): void {
    this.clearAmenityMarkers();

    if (!this.map) {
      return;
    }

    for (const amenity of amenities) {
      const element = createAmenityMarkerElement(getAmenityIcon(amenity.type), amenity.name);
      element.tabIndex = 0;
      this.bindAmenityMarkerHover(element, amenity);

      const marker = new Marker({ element, anchor: 'bottom' })
        .setLngLat([amenity.lng, amenity.lat])
        .addTo(this.map);

      this.amenityMarkerEntries.push({ marker, element, amenity });
    }

    this.applyAmenityMarkerZoomMode();
  }

  private applyAmenityMarkerZoomMode(): void {
    if (!this.map || this.amenityMarkerEntries.length === 0) {
      return;
    }

    const display = amenityMarkerDisplayForZoom(this.map.getZoom());
    if (display === 'dot') {
      this.hideAmenityPopupImmediate();
    }

    for (const entry of this.amenityMarkerEntries) {
      setAmenityMarkerDisplay(entry.element, display);
    }
  }

  private clearAmenityMarkers(): void {
    this.hideAmenityPopupImmediate();

    for (const entry of this.amenityMarkerEntries) {
      entry.marker.remove();
    }
    this.amenityMarkerEntries = [];
  }

  private bindAmenityMarkerHover(element: HTMLDivElement, amenity: NearbyAmenity): void {
    const showPopup = (): void => {
      this.cancelAmenityPopupHide();

      if (this.amenityHoverPopup && this.amenityHoverPopupAmenityId === amenity.id) {
        return;
      }

      const display = element.dataset['display'] ?? 'dot';
      const popupOffset =
        display === 'pin' ? this.amenityPopupOffsetPin : this.amenityPopupOffsetDot;

      this.hideAmenityPopupImmediate();
      this.amenityHoverPopup = new Popup({
        closeButton: false,
        closeOnClick: false,
        anchor: 'bottom',
        offset: popupOffset,
        className: 'amenity-map-popup',
      })
        .setLngLat([amenity.lng, amenity.lat])
        .setHTML(this.amenityPopupHtml(amenity))
        .addTo(this.map);
      this.amenityHoverPopupAmenityId = amenity.id;
      this.elevateAmenityPopup();
      this.attachAmenityPopupHoverHandlers();
    };

    element.addEventListener('mouseenter', showPopup);
    element.addEventListener('mouseleave', () => this.scheduleAmenityPopupHide());
    element.addEventListener('focus', showPopup);
    element.addEventListener('blur', () => this.scheduleAmenityPopupHide());
  }

  /** Keep hover popups above deck.gl layers, markers, and map controls. */
  private elevateAmenityPopup(): void {
    const popupElement = this.amenityHoverPopup?.getElement();
    if (!popupElement || !this.map) {
      return;
    }

    popupElement.style.zIndex = '10';
    this.map.getContainer().appendChild(popupElement);
  }

  private attachAmenityPopupHoverHandlers(): void {
    const popupElement = this.amenityHoverPopup?.getElement();
    if (!popupElement) {
      return;
    }

    popupElement.addEventListener('mouseenter', () => this.cancelAmenityPopupHide());
    popupElement.addEventListener('mouseleave', () => this.scheduleAmenityPopupHide());
  }

  private cancelAmenityPopupHide(): void {
    if (this.amenityPopupHideTimer) {
      clearTimeout(this.amenityPopupHideTimer);
      this.amenityPopupHideTimer = null;
    }
  }

  private scheduleAmenityPopupHide(): void {
    this.cancelAmenityPopupHide();
    this.amenityPopupHideTimer = setTimeout(() => {
      this.amenityPopupHideTimer = null;
      this.hideAmenityPopupImmediate();
    }, this.amenityPopupHideDelayMs);
  }

  private hideAmenityPopupImmediate(): void {
    this.cancelAmenityPopupHide();
    this.amenityHoverPopup?.remove();
    this.amenityHoverPopup = null;
    this.amenityHoverPopupAmenityId = null;
  }

  private amenityPopupHtml(amenity: NearbyAmenity): string {
    const address = amenity.address || this.translate.instant('regions.noAddress');
    const distance = this.translate.instant('regions.distanceAway', {
      distance: Math.round(amenity.distanceMeters),
    });
    const name = this.escapeHtml(amenity.name);
    const addressHtml = this.escapeHtml(address);
    const typeHtml = this.escapeHtml(amenity.type);
    const distanceHtml = this.escapeHtml(distance);

    return `
      <div class="amenity-map-popup-body">
        <div class="amenity-map-popup-name">${name}</div>
        <div class="amenity-map-popup-address">${addressHtml}</div>
        <div class="amenity-map-popup-meta">
          <span class="amenity-map-popup-type">${typeHtml}</span>
          <span class="amenity-map-popup-distance">${distanceHtml}</span>
        </div>
      </div>
    `;
  }

  private escapeHtml(value: string): string {
    return value
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  private updateDeckLayers(regions: RegionOfInterest[], activeRegionId: string): void {
    const sortedRegions = [...regions].sort((a, b) => {
      const aActive = a.id === activeRegionId;
      const bActive = b.id === activeRegionId;
      if (aActive !== bActive) {
        return aActive ? 1 : -1;
      }
      return b.radius - a.radius;
    });

    const circleData = sortedRegions.map((region) => {
      const baseColor = this.hexToRgb(region.color);
      const isActive = region.id === activeRegionId;

      return {
        polygon: this.createCirclePolygon(region.lng, region.lat, region.radius),
        fillColor: [baseColor[0], baseColor[1], baseColor[2], isActive ? 56 : 32] as [
          number,
          number,
          number,
          number,
        ],
        lineColor: [baseColor[0], baseColor[1], baseColor[2], isActive ? 230 : 160] as [
          number,
          number,
          number,
          number,
        ],
      };
    });

    const layers = [
      new PolygonLayer({
        id: 'region-circles-fill',
        data: circleData,
        getPolygon: (d: { polygon: [number, number][] }) => d.polygon,
        getFillColor: (d: { fillColor: [number, number, number, number] }) => d.fillColor,
        getLineColor: (d: { lineColor: [number, number, number, number] }) => d.lineColor,
        getLineWidth: 2,
        lineWidthUnits: 'pixels',
        filled: true,
        stroked: true,
        pickable: false,
        parameters: {
          depthTest: false,
        },
      }),
    ];

    this.deckOverlay.setProps({ layers });
  }

  private createCirclePolygon(lng: number, lat: number, radiusMeters: number): [number, number][] {
    const points = 64;
    const coords: [number, number][] = [];

    for (let i = 0; i <= points; i++) {
      const angle = (i / points) * (2 * Math.PI);
      const dx = radiusMeters * Math.cos(angle);
      const dy = radiusMeters * Math.sin(angle);

      const dLat = dy / 111320;
      const dLng = dx / (111320 * Math.cos((lat * Math.PI) / 180));

      coords.push([lng + dLng, lat + dLat]);
    }

    return coords;
  }

  private hexToRgb(hexColor: string): [number, number, number] {
    const normalized = hexColor.replace('#', '').trim();
    const shortHex = normalized.length === 3;

    if (![3, 6].includes(normalized.length)) {
      return [99, 102, 241];
    }

    const full = shortHex
      ? normalized
          .split('')
          .map((char) => char + char)
          .join('')
      : normalized;

    const value = Number.parseInt(full, 16);
    if (Number.isNaN(value)) {
      return [99, 102, 241];
    }

    return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
  }

  private async handleMarkerDragEnd(): Promise<void> {
    const lngLat = this.marker.getLngLat();
    await this.moveActiveRegionTo(lngLat.lng, lngLat.lat);
  }

  private async moveActiveRegionTo(lng: number, lat: number): Promise<void> {
    const clamped = clampToSwitzerland(lng, lat);
    this.marker.setLngLat([clamped.lng, clamped.lat]);
    this.locationService.setLocation(clamped.lat, clamped.lng);

    this.reverseAbort?.abort();
    const abort = new AbortController();
    this.reverseAbort = abort;

    try {
      const locality = await this.reverseGeocodeLocality(clamped.lat, clamped.lng, abort);
      if (this.reverseAbort !== abort) {
        return;
      }
      this.locationService.setLocation(clamped.lat, clamped.lng, '', locality ?? undefined);
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        return;
      }
      this.locationService.setLocation(clamped.lat, clamped.lng, '');
    } finally {
      if (this.reverseAbort === abort) {
        this.reverseAbort = null;
      }
    }
  }

  private async applyReverseAutoName(lat: number, lng: number): Promise<void> {
    this.reverseAbort?.abort();
    const abort = new AbortController();
    this.reverseAbort = abort;
    try {
      const locality = await this.reverseGeocodeLocality(lat, lng, abort);
      if (this.reverseAbort !== abort || !locality) {
        return;
      }
      this.locationService.setLocation(lat, lng, undefined, locality);
    } catch (error) {
      if (!(error instanceof DOMException && error.name === 'AbortError')) {
        console.warn('Initial reverse geocoding failed:', error);
      }
    } finally {
      if (this.reverseAbort === abort) {
        this.reverseAbort = null;
      }
    }
  }

  private async reverseGeocodeLocality(
    lat: number,
    lng: number,
    abort: AbortController,
  ): Promise<string | null> {
    const reverse = await this.geocodingService.reverseGeocode(lat, lng, abort.signal);
    return reverse?.locality ?? null;
  }
}
