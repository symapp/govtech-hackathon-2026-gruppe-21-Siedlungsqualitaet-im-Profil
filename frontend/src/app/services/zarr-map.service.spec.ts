import { describe, expect, it } from 'vitest';
import {
  computeLayerDisplayPlan,
  singleLayerOpacityFromImportance,
  singleLayerOpacityFromSlider,
} from '../utils/zarr-map-display.util';
import type { LayerPreference } from '../models/layer-preference.model';

function preference(overrides: Partial<LayerPreference> = {}): LayerPreference {
  return {
    enabled: true,
    importance: 50,
    rangeMin: 0.25,
    rangeMax: 0.75,
    falloffLeft: 0.1,
    falloffRight: 0.1,
    ...overrides,
  };
}

describe('zarr map helpers', () => {
  it('scales single-layer opacity by importance', () => {
    expect(singleLayerOpacityFromImportance(0.82, preference({ importance: 75 }))).toBeCloseTo(
      0.615,
    );
  });

  it('scales single-layer opacity by the overview slider value', () => {
    expect(
      singleLayerOpacityFromSlider(0.82, preference({ importance: 75 }), 35 / 100),
    ).toBeCloseTo(0.21525);
  });

  it('shows a non-overview layer when it is the only active layer', () => {
    const plan = computeLayerDisplayPlan({ 'vacancy-rates': preference() }, [
      { id: 'vacancy-rates', ready: true, includeInOverview: false },
    ]);

    expect(plan).toEqual({
      hasOverview: false,
      visibleLayerIds: ['vacancy-rates'],
    });
  });

  it('keeps the overview visible when a non-overview layer is mixed with overview layers', () => {
    const plan = computeLayerDisplayPlan(
      {
        'vacancy-rates': preference(),
        tranquillity: preference(),
      },
      [
        { id: 'vacancy-rates', ready: true, includeInOverview: false },
        { id: 'tranquillity', ready: true, includeInOverview: true },
      ],
    );

    expect(plan).toEqual({
      hasOverview: true,
      visibleLayerIds: [],
    });
  });
});
