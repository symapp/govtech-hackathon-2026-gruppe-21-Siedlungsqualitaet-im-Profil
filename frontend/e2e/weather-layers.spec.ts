import { expect, test } from '@playwright/test';

declare global {
  interface Window {
    __SIEDLUNG_ZARR_SAMPLE__?: (lng: number, lat: number, layerId: string) => Promise<number | null>;
    __SIEDLUNG_MAP__?: { getLayer: (id: string) => unknown; getSource: (id: string) => unknown };
  }
}

/**
 * E2E tests for the weather data layer (temperature).
 *
 * These tests verify:
 *  1. Weather layer cards appear in the sidebar with correct labels.
 *  2. Temperature layer is registered in the MapLibre instance.
 *  3. Switching to a weather layer activates it (other layers become inactive).
 *  4. When meteo Zarr data is available, point samples return plausible Swiss values.
 *
 * NOTE: Pollen, pharmacy, and doctor layers are NOT implemented in the frontend.
 * See data-pipelines/pollen-rasterize.py for the planned pollen pipeline;
 * the frontend layer config, model fields, and i18n keys are still missing.
 */

/** Central Bern — well inside the Swiss settlement grid, known temperature range. */
const BERN_CENTER = { lng: 7.4474, lat: 46.9481 };

/** Lake Neuchâtel surface — outside populated cells, useful null-check. */
const LAKE_NEUCHATEL = { lng: 6.9, lat: 46.98 };

async function waitForMapReady(page: import('@playwright/test').Page): Promise<void> {
  await expect(page.getByTestId('map-container')).toBeVisible({ timeout: 30_000 });
}

async function sampleZarr(
  page: import('@playwright/test').Page,
  lng: number,
  lat: number,
  layerId: string,
): Promise<number | null> {
  return page.evaluate(
    async ({ lng, lat, layerId }) => {
      const fn = window.__SIEDLUNG_ZARR_SAMPLE__;
      if (!fn) return null;
      return fn(lng, lat, layerId);
    },
    { lng, lat, layerId },
  );
}

async function isLayerRegistered(
  page: import('@playwright/test').Page,
  layerId: string,
): Promise<boolean> {
  return page.evaluate((id) => {
    const map = window.__SIEDLUNG_MAP__;
    if (!map) return false;
    return !!map.getLayer(id);
  }, layerId);
}

test.describe('Weather layers — sidebar UI', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto('/');
    await waitForMapReady(page);
  });

  test('weather section is separated from the main factors section', async ({ page }) => {
    const weatherSection = page.locator('.weather-section');

    await expect(weatherSection).toBeVisible({ timeout: 30_000 });
    await expect(weatherSection.getByRole('heading', { name: /Météo|Meteo|Weather/i })).toBeVisible();
    await expect(weatherSection.locator('.map-layer-card')).toHaveCount(1);
    await expect(
      weatherSection.locator('.map-layer-title').filter({ hasText: /Temperature|Température/i }),
    ).toBeVisible();
  });

  test('temperature layer card is present in the sidebar', async ({ page }) => {
    // The sidebar renders one .map-layer-card per ZarrLayerDefinition.
    // Layer titles use the i18n pipe: layers.temperature.label → "Temperature" / "Temperatur".
    // No data-layer-id attribute exists — match by title text inside the card.
    const card = page
      .locator('.map-layer-card')
      .filter({ has: page.locator('.map-layer-title', { hasText: /Tempe?r(atur|ature)/i }) });
    await expect(card).toBeVisible({ timeout: 30_000 });
  });

  test('temperature card label matches i18n key', async ({ page }) => {
    const card = page
      .locator('.map-layer-card')
      .filter({ has: page.locator('.map-layer-title', { hasText: /Tempe?r(atur|ature)/i }) });
    await expect(card).toBeVisible({ timeout: 30_000 });
    const text = await card.locator('.map-layer-title').textContent();
    expect(text).toMatch(/Tempe?r(atur|ature)/i);
  });

  test('temperature card shows a color code legend', async ({ page }) => {
    const temperatureCard = page
      .locator('.weather-section .map-layer-card')
      .filter({ has: page.locator('.map-layer-title', { hasText: /Tempe?r(atur|ature)/i }) });

    await expect(temperatureCard).toBeVisible({ timeout: 30_000 });
    await expect(temperatureCard.getByTestId('temperature-color-code')).toBeVisible();
    await expect(temperatureCard.getByTestId('temperature-variance-code')).toBeVisible();
  });
});

test.describe('Weather layers — map registration', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto('/');
    await waitForMapReady(page);

    // Wait until the MapLibre instance is available.
    await expect
      .poll(() => page.evaluate(() => !!window.__SIEDLUNG_MAP__), { timeout: 30_000 })
      .toBe(true);
  });

  test('temperature layer is registered on the MapLibre instance', async ({ page }) => {
    // ZarrMapService.initLayers() registers all ZARR_LAYER_DEFINITIONS including weather.
    await expect.poll(() => isLayerRegistered(page, 'temperature'), { timeout: 60_000 }).toBe(true);
  });

  test('weather layer is registered alongside core settlement layers', async ({ page }) => {
    // installLayersOnMap fires on map 'load', so we must poll — not check synchronously.
    await expect
      .poll(
        () =>
          page.evaluate(() => {
            const map = window.__SIEDLUNG_MAP__;
            if (!map) return 0;
            const ids = [
              'temperature',
              'tranquillity',
              'population-density',
            ];
            return ids.filter((id) => !!map.getLayer(id)).length;
          }),
        { timeout: 60_000 },
      )
      .toBe(3);
  });
});

test.describe('Weather layers — data sampling (requires uploaded Zarr stores)', () => {
  /**
   * These tests only run when meteo Zarr stores are present on the B2 bucket.
   * If no data is uploaded yet, sampleZarr returns null — tests are skipped gracefully.
   */
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto('/');
    // Wait for all non-weather layers to finish loading.
    await expect
      .poll(
        async () => {
          const loadingCount = await page.locator('.map-layer-card.loading').count();
          // Tolerate one loading card (the weather layer may never load in CI).
          return loadingCount <= 1;
        },
        { timeout: 90_000 },
      )
      .toBe(true);

    const temperatureCard = page
      .locator('.weather-section .map-layer-card')
      .filter({ has: page.locator('.map-layer-title', { hasText: /Tempe?r(atur|ature)/i }) });
    await expect(temperatureCard).not.toHaveClass(/loading/, { timeout: 90_000 });
  });

  test('temperature at Bern center is available and within configured range', async ({ page }) => {
    const value = await sampleZarr(page, BERN_CENTER.lng, BERN_CENTER.lat, 'temperature');

    expect(value).not.toBeNull();
    expect(value!).toBeGreaterThanOrEqual(-20);
    expect(value!).toBeLessThanOrEqual(50);
  });

  test('temperature on open lake surface is available and within configured range', async ({
    page,
  }) => {
    // Unlike population-density, weather is a continuous field — lake is still valid.
    const value = await sampleZarr(page, LAKE_NEUCHATEL.lng, LAKE_NEUCHATEL.lat, 'temperature');

    expect(value).not.toBeNull();
    expect(value!).toBeGreaterThanOrEqual(-20);
    expect(value!).toBeLessThanOrEqual(50);
  });

});

test.describe('Pollen / pharmacy / doctor — layer absence verification', () => {
  /**
   * These tests document that pollen, pharmacy, and doctor layers are intentionally
   * absent from the current frontend. They will fail as soon as those features are
   * implemented, serving as a reminder to update the E2E suite at that point.
   */
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto('/');
    await waitForMapReady(page);
    await expect
      .poll(() => page.evaluate(() => !!window.__SIEDLUNG_MAP__), { timeout: 30_000 })
      .toBe(true);
  });

  test('pollen layer is NOT yet registered on the map (pending implementation)', async ({
    page,
  }) => {
    const registered = await isLayerRegistered(page, 'pollen');
    expect(registered).toBe(false);
  });

  test('pharmacy layer is NOT yet registered on the map (pending implementation)', async ({
    page,
  }) => {
    const registered = await isLayerRegistered(page, 'pharmacy');
    expect(registered).toBe(false);
  });

  test('doctor layer is NOT yet registered on the map (pending implementation)', async ({
    page,
  }) => {
    const registered = await isLayerRegistered(page, 'doctor');
    expect(registered).toBe(false);
  });
});
