import type { LayerPreference } from '../models/layer-preference.model';

export interface ManagedLayerDisplayState {
  id: string;
  ready: boolean;
  includeInOverview: boolean;
}

export interface LayerDisplayPlan {
  hasOverview: boolean;
  visibleLayerIds: string[];
}

export function isOverviewEligible(includeInOverview: boolean | undefined): boolean {
  return includeInOverview !== false;
}

export function computeLayerDisplayPlan(
  preferences: Record<string, LayerPreference>,
  managedStates: readonly ManagedLayerDisplayState[],
): LayerDisplayPlan {
  const activeReadyLayerIds = managedStates
    .filter((state) => {
      const pref = preferences[state.id];
      return !!pref?.enabled && pref.importance > 0 && state.ready;
    })
    .map((state) => state.id);

  const overviewEligibleActiveReadyLayerIds = managedStates
    .filter((state) => {
      const pref = preferences[state.id];
      return !!pref?.enabled && pref.importance > 0 && state.ready && state.includeInOverview;
    })
    .map((state) => state.id);

  if (activeReadyLayerIds.length === 1) {
    return {
      hasOverview: false,
      visibleLayerIds: activeReadyLayerIds,
    };
  }

  if (activeReadyLayerIds.length > 1 && overviewEligibleActiveReadyLayerIds.length > 0) {
    return {
      hasOverview: true,
      visibleLayerIds: [],
    };
  }

  if (overviewEligibleActiveReadyLayerIds.length === 1) {
    return {
      hasOverview: false,
      visibleLayerIds: overviewEligibleActiveReadyLayerIds,
    };
  }

  return {
    hasOverview: false,
    visibleLayerIds: [],
  };
}

export function singleLayerOpacityFromImportance(
  baseOpacity: number,
  preference: LayerPreference | undefined,
): number {
  if (!preference?.enabled || preference.importance <= 0) {
    return 0;
  }
  const clampedBaseOpacity = Math.min(1, Math.max(0, baseOpacity));
  const clampedImportance = Math.min(100, Math.max(0, preference.importance));
  const importanceFactor = clampedImportance / 100;
  return clampedBaseOpacity * importanceFactor;
}

export function singleLayerOpacityFromSlider(
  baseOpacity: number,
  preference: LayerPreference | undefined,
  sliderOpacity: number,
): number {
  const clampedSliderOpacity = Math.min(1, Math.max(0, sliderOpacity));
  return singleLayerOpacityFromImportance(baseOpacity, preference) * clampedSliderOpacity;
}
