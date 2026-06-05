import { expect, test } from '@playwright/test';

/** Read pixels from the MapLibre WebGL canvas (2d context cannot be used). */
async function sampleWebglCenterAlpha(page: import('@playwright/test').Page): Promise<number> {
  return page.evaluate(() => {
    const canvas = document.querySelector(
      '.maplibregl-canvas',
    ) as HTMLCanvasElement | null;
    if (!canvas) {
      return 0;
    }

    const gl =
      canvas.getContext('webgl2', { preserveDrawingBuffer: true }) ??
      canvas.getContext('webgl', { preserveDrawingBuffer: true });
    if (!gl) {
      return 0;
    }

    const cx = Math.floor(canvas.width / 2);
    const cy = Math.floor(canvas.height / 2);
    const pixels = new Uint8Array(4);
    gl.readPixels(cx, cy, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    return pixels[3];
  });
}

test.describe('Zarr map overlay', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
  });

  test('registers zarr layers on the map', async ({ page }) => {
    await page.goto('/');

    await expect(page.getByTestId('map-container')).toBeVisible();

    await expect
      .poll(
        async () =>
          page.evaluate(() => {
            const map = window.__SIEDLUNG_MAP__;
            if (!map) {
              return 0;
            }
            let count = 0;
            if (map.getLayer('tranquillity')) {
              count += 1;
            }
            if (map.getLayer('population-density')) {
              count += 1;
            }
            return count;
          }),
        { timeout: 90_000 },
      )
      .toBe(2);
  });

  test('factor layers finish loading', async ({ page }) => {
    await page.goto('/');

    await expect(page.getByTestId('map-container')).toBeVisible();

    await expect
      .poll(async () => page.locator('.map-layer-card.loading').count(), { timeout: 90_000 })
      .toBe(0);
  });

  test('map canvas has drawn content after zarr render', async ({ page }) => {
    await page.goto('/');

    await expect(page.locator('.maplibregl-canvas')).toBeVisible();

    // Wait for layers to load, then allow a few frames for WebGL paint.
    await expect
      .poll(async () => page.locator('.map-layer-card.loading').count(), { timeout: 90_000 })
      .toBe(0);

    await page.waitForTimeout(2_000);

    await expect
      .poll(async () => sampleWebglCenterAlpha(page), { timeout: 30_000 })
      .toBeGreaterThan(0);
  });

  test('preference editor and overview score appear after zarr sampling', async ({ page }) => {
    await page.goto('/');

    await expect(page.getByRole('heading', { name: 'Gesamtübersicht', exact: true })).toBeVisible();

    await expect
      .poll(
        async () => {
          const text = await page.locator('.region-score-value').first().textContent();
          return text?.trim() ?? '';
        },
        { timeout: 90_000 },
      )
      .not.toBe('—');

    await expect(page.locator('.trapezoid-svg').first()).toBeVisible();
  });

  test('overview generation advances after map is ready', async ({ page }) => {
    await page.goto('/');

    await expect
      .poll(async () => page.locator('.map-layer-card.loading').count(), { timeout: 90_000 })
      .toBe(0);

    await expect
      .poll(async () => {
        return page.evaluate(() => window.__SIEDLUNG_OVERVIEW__?.generation ?? 0);
      }, { timeout: 60_000 })
      .toBeGreaterThan(0);
  });

  test('preference change rescored without extra zarr fetches when cached', async ({ page }) => {
    await page.goto('/');

    await expect
      .poll(async () => page.locator('.map-layer-card.loading').count(), { timeout: 90_000 })
      .toBe(0);

    await expect
      .poll(async () => page.evaluate(() => window.__SIEDLUNG_OVERVIEW__?.generation ?? 0), {
        timeout: 60_000,
      })
      .toBeGreaterThan(0);

    await expect
      .poll(async () => page.evaluate(() => window.__SIEDLUNG_OVERVIEW__?.loading ?? true), {
        timeout: 90_000,
      })
      .toBe(false);

    const before = await page.evaluate(() => ({
      gen: window.__SIEDLUNG_OVERVIEW__?.generation ?? 0,
      zarrRequests: performance
        .getEntriesByType('resource')
        .filter((e) => /zarr|minio|egov-hackathon/i.test(e.name)).length,
    }));

    const slider = page.locator('.layer-weight-slider').first();
    await slider.focus();
    const box = await slider.boundingBox();
    if (box) {
      await page.mouse.click(box.x + box.width * 0.3, box.y + box.height / 2);
    }

    await expect
      .poll(async () => page.evaluate(() => window.__SIEDLUNG_OVERVIEW__?.generation ?? 0), {
        timeout: 15_000,
      })
      .toBeGreaterThan(before.gen);

    const after = await page.evaluate(() =>
      performance
        .getEntriesByType('resource')
        .filter((e) => /zarr|minio|egov-hackathon/i.test(e.name)).length,
    );

    expect(after - before.zarrRequests).toBeLessThan(30);
  });

  test('national overview rebuilds after zooming out', async ({ page }) => {
    test.setTimeout(300_000);

    await page.goto('/');

    await expect
      .poll(async () => page.locator('.map-layer-card.loading').count(), { timeout: 90_000 })
      .toBe(0);

    const genBefore = await page.evaluate(() => window.__SIEDLUNG_OVERVIEW__?.generation ?? 0);

    await page.evaluate(() => {
      const map = window.__SIEDLUNG_MAP__;
      if (!map) {
        return;
      }
      map.resize();
      map.jumpTo({ center: [8.23, 46.82], zoom: 7.5 });
      window.__SIEDLUNG_SYNC_OVERVIEW__?.();
    });

    await expect
      .poll(async () => page.evaluate(() => window.__SIEDLUNG_OVERVIEW__?.generation ?? 0), {
        timeout: 240_000,
      })
      .toBeGreaterThan(genBefore);

    await expect
      .poll(async () => page.evaluate(() => window.__SIEDLUNG_OVERVIEW__?.loading ?? true), {
        timeout: 240_000,
      })
      .toBe(false);
  });
});
