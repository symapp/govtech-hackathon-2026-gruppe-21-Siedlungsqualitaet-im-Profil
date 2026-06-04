import { describe, expect, it } from 'vitest';
import {
  groupZarrLayerDefinitions,
  ZARR_LAYER_DEFINITIONS,
} from './zarr-layers.config';

const EXPECTED_TEMPERATURE_COLORMAP = [
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
] as const;

describe('zarr layer config', () => {
  it('groups weather layers into a dedicated sidebar section', () => {
    const groups = groupZarrLayerDefinitions();
    const weatherGroup = groups.find((group) => group.id === 'weather');

    expect(groups[0]?.id).toBe('general');
    expect(weatherGroup).toBeDefined();
    expect(weatherGroup?.titleKey).toBe('sidebar.weatherTitle');
    expect(weatherGroup?.layers.map((layer) => layer.id)).toEqual(['temperature']);
  });

  it('keeps everything in the general section when no weather layers are present', () => {
    const groups = groupZarrLayerDefinitions(
      ZARR_LAYER_DEFINITIONS.filter((layer) => layer.sidebarGroup !== 'weather'),
    );

    expect(groups).toHaveLength(1);
    expect(groups[0]?.id).toBe('general');
    expect(groups[0]?.layers.every((layer) => layer.sidebarGroup !== 'weather')).toBe(true);
  });

  it('keeps the thermal ramp cold-blue to mild-green to hot-red', () => {
    const temperature = ZARR_LAYER_DEFINITIONS.find((layer) => layer.id === 'temperature');

    expect(temperature?.colormap).toEqual(EXPECTED_TEMPERATURE_COLORMAP);
  });

  it('keeps temperature ramp free of white stops and enforces lower temperature opacity', () => {
    const temperature = ZARR_LAYER_DEFINITIONS.find((layer) => layer.id === 'temperature');

    expect(temperature).toBeDefined();
    expect(temperature?.colormap.some((stop) => stop.toLowerCase() === '#ffffff')).toBe(false);
    expect(temperature?.renderOpacity).toBe(0.4);
    expect(temperature?.clim).toEqual([-20, 50]);
  });

  it('keeps the banded 5/5/5 cold-mild-hot palette grouping', () => {
    const temperature = ZARR_LAYER_DEFINITIONS.find((layer) => layer.id === 'temperature');

    expect(temperature).toBeDefined();
    // 15 colors: 5 blue, 5 green, 5 red.
    expect(temperature?.colormap).toHaveLength(15);

    expect(temperature?.colormap.slice(0, 5)).toEqual(EXPECTED_TEMPERATURE_COLORMAP.slice(0, 5));
    expect(temperature?.colormap.slice(5, 10)).toEqual(EXPECTED_TEMPERATURE_COLORMAP.slice(5, 10));
    expect(temperature?.colormap.slice(10, 15)).toEqual(EXPECTED_TEMPERATURE_COLORMAP.slice(10, 15));
  });

  it('only temperature uses explicit renderOpacity override', () => {
    const temperature = ZARR_LAYER_DEFINITIONS.find((layer) => layer.id === 'temperature');
    const tranquillity = ZARR_LAYER_DEFINITIONS.find((layer) => layer.id === 'tranquillity');

    expect(temperature?.renderOpacity).toBe(0.4);
    expect(temperature?.includeInOverview).toBe(false);
    expect(tranquillity?.renderOpacity).toBeUndefined();
  });
});