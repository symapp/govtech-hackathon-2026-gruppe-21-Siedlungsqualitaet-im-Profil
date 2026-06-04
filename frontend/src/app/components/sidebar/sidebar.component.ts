import { Component, computed, DestroyRef, inject, OnInit, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { TranslatePipe, TranslateService } from '@ngx-translate/core';
import { LanguageSelectorComponent } from '../language-selector/language-selector.component';
import {
  LIFESTYLE_PRESETS,
  loadStoredLifestylePresetId,
  type LifestylePresetId,
} from '../../config/lifestyle-presets.config';
import {
  OVERVIEW_COLORMAP,
  groupZarrLayerDefinitions,
  ZARR_LAYER_DEFINITIONS,
  type ZarrLayerDefinition,
} from '../../config/zarr-layers.config';
import { AMENITY_CATEGORIES } from '../../config/amenity-categories.config';
import type { LayerPreference } from '../../models/layer-preference.model';
import { LocationService } from '../../services/location.service';
import { createGoodPlaceLayerPreference } from '../../config/good-place-defaults.config';
import {
  importanceFromStars,
  isDealbreakerPreference,
  normalizationBoundsForLayer,
  setDealbreakerFloors,
  starsFromImportance,
} from '../../utils/preference-scoring.util';
import { TrapezoidPreferenceEditorComponent } from '../trapezoid-preference-editor/trapezoid-preference-editor.component';
import { MapPanelsService } from '../../services/map-panels.service';

@Component({
  selector: 'app-sidebar',
  standalone: true,
  imports: [
    FormsModule,
    RouterLink,
    TranslatePipe,
    LanguageSelectorComponent,
    TrapezoidPreferenceEditorComponent,
  ],
  templateUrl: './sidebar.component.html',
  styleUrl: './sidebar.component.scss',
})
export class SidebarComponent implements OnInit {
  protected readonly locationService = inject(LocationService);
  private readonly translate = inject(TranslateService);
  private readonly mapPanels = inject(MapPanelsService);
  private readonly destroyRef = inject(DestroyRef);
  protected isCollapsed = this.mapPanels.initialCollapsed();
  /** Per-layer advanced panel (curve editor + dealbreaker). */
  protected readonly advancedOpenByLayerId = signal<Record<string, boolean>>({});
  protected readonly metricDefinitions = ZARR_LAYER_DEFINITIONS;
  protected readonly amenityCategories = AMENITY_CATEGORIES;
  protected readonly lifestylePresets = LIFESTYLE_PRESETS;
  protected readonly activePresetId = signal<LifestylePresetId>(loadStoredLifestylePresetId());

  private readonly layerGroups = groupZarrLayerDefinitions();
  private readonly weatherLayerIds = new Set(
    this.layerGroups.find((group) => group.id === 'weather')?.layers.map((layer) => layer.id) ??
      [],
  );
  protected readonly generalLayers = computed(() =>
    this.locationService.zarrLayers().filter((layer) => !this.weatherLayerIds.has(layer.id)),
  );
  protected readonly weatherLayers = computed(() =>
    this.locationService.zarrLayers().filter((layer) => this.weatherLayerIds.has(layer.id)),
  );

  ngOnInit(): void {
    this.mapPanels.setRightOpen(!this.isCollapsed);
    this.mapPanels.closeRight$.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(() => {
      this.isCollapsed = true;
      this.mapPanels.setRightOpen(false);
    });
    this.mapPanels.openRight$.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(() => {
      this.isCollapsed = false;
      this.mapPanels.setRightOpen(true);
    });
  }

  toggleSidebar(): void {
    if (this.isCollapsed) {
      this.mapPanels.notifyOpen('right');
    }
    this.isCollapsed = !this.isCollapsed;
    this.mapPanels.setRightOpen(!this.isCollapsed);
  }

  metricUnit(def: ZarrLayerDefinition): string {
    return this.translate.instant(def.metricUnitKey);
  }

  temperatureLegendAriaLabel(def: ZarrLayerDefinition): string {
    return `${this.translate.instant(def.metricLabelKey)} gradient from ${def.formatValue(def.clim[0])} to ${def.formatValue(def.clim[1])} ${this.metricUnit(def)}`;
  }

  legendGradient(colors: readonly string[] | string[]): string {
    return `linear-gradient(to right, ${colors.join(', ')})`;
  }

  overviewLegendGradient(): string {
    return this.legendGradient([...OVERVIEW_COLORMAP]);
  }

  layerDefinition(layerId: string): ZarrLayerDefinition | undefined {
    return this.metricDefinitions.find((d) => d.id === layerId);
  }

  normalizationBounds(layerId: string) {
    const layer = this.locationService.zarrLayers().find((l) => l.id === layerId);
    const def = this.layerDefinition(layerId);
    if (!def) {
      return { p5: 0, p95: 1, higherIsBetter: true };
    }
    return normalizationBoundsForLayer(def.clim, def.higherIsBetter, layer?.meta);
  }

  onPreferenceChange(layerId: string, preference: LayerPreference): void {
    this.locationService.setZarrLayerPreference(layerId, preference);
  }

  importanceStars(layerId: string): number {
    const layer = this.locationService.zarrLayers().find((l) => l.id === layerId);
    return layer ? starsFromImportance(layer.preference.importance) : 0;
  }

  onImportanceStarsChange(layerId: string, stars: number): void {
    const layer = this.locationService.zarrLayers().find((l) => l.id === layerId);
    if (!layer) {
      return;
    }
    this.locationService.setZarrLayerPreference(layerId, {
      ...layer.preference,
      importance: importanceFromStars(stars),
    });
  }

  isDealbreaker(layerId: string): boolean {
    const layer = this.locationService.zarrLayers().find((l) => l.id === layerId);
    return layer ? isDealbreakerPreference(layer.preference) : false;
  }

  onDealbreakerChange(layerId: string, event: Event): void {
    const input = event.target as HTMLInputElement;
    const layer = this.locationService.zarrLayers().find((l) => l.id === layerId);
    if (!layer) {
      return;
    }
    this.locationService.setZarrLayerPreference(
      layerId,
      setDealbreakerFloors(layer.preference, input.checked),
    );
  }

  onLayerEnabledChange(layerId: string, event: Event): void {
    const input = event.target as HTMLInputElement;
    this.locationService.setZarrLayerEnabled(layerId, input.checked);
  }

  onOverviewOpacityChange(value: number): void {
    this.locationService.setOverviewOpacity(value);
  }

  onPresetChange(event: Event): void {
    const select = event.target as HTMLSelectElement;
    const presetId = select.value as LifestylePresetId;
    this.activePresetId.set(presetId);
    this.locationService.applyLifestylePreset(presetId);
  }

  resetLayerPreference(layerId: string): void {
    this.locationService.resetZarrLayerPreference(layerId);
  }

  resetAllPreferences(): void {
    this.locationService.resetAllZarrPreferences();
    this.activePresetId.set(loadStoredLifestylePresetId());
  }

  deselectAllLayers(): void {
    this.locationService.setAllZarrLayersEnabled(false);
  }

  isAnyLayerEnabled(): boolean {
    return this.locationService.zarrLayers().some((layer) => layer.enabled);
  }

  onAmenitiesMasterToggle(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.locationService.setAmenitiesEnabled(input.checked);
  }

  amenityImportanceStars(categoryId: string): number {
    const pref = this.locationService.getZarrLayerPreference(categoryId);
    return pref ? starsFromImportance(pref.importance) : 0;
  }

  onAmenityImportanceChange(categoryId: string, stars: number): void {
    const pref = this.locationService.getZarrLayerPreference(categoryId);
    this.locationService.setZarrLayerPreference(categoryId, {
      ...(pref ?? createGoodPlaceLayerPreference(categoryId)),
      importance: importanceFromStars(stars),
    });
  }

  amenityCountForCategory(categoryId: string): number {
    const cat = AMENITY_CATEGORIES.find((c) => c.id === categoryId);
    if (!cat) return 0;
    const activeRegionId = this.locationService.activeRegionId();
    return this.locationService.amenityCountByCategoryForRegion(activeRegionId, cat.categoryKey);
  }

  isLayerAdvancedOpen(layerId: string): boolean {
    return this.advancedOpenByLayerId()[layerId] === true;
  }

  toggleLayerAdvanced(layerId: string, event: Event): void {
    event.stopPropagation();
    this.advancedOpenByLayerId.update((current) => ({
      ...current,
      [layerId]: !current[layerId],
    }));
  }

  shouldShowAdvancedControls(layerId: string): boolean {
    return layerId !== 'temperature';
  }
}
