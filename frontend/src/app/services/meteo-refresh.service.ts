import { HttpClient } from '@angular/common/http';
import { DestroyRef, Injectable, inject, signal } from '@angular/core';
import { environment } from '../../environments/environment';
import type { MeteoManifest } from '../models/meteo-manifest.model';
import { ZarrMapService } from './zarr-map.service';

const POLL_INTERVAL_MS = 10 * 60 * 1000; // 10 minutes
const MANIFEST_URL = `${environment.zarrBaseUrl}/meteo_manifest.json`;

/**
 * Polls meteo_manifest.json on B2 every 10 minutes.
 * When the `last_updated` timestamp changes, triggers a reload of the meteo
 * Zarr layers in ZarrMapService so the frontend picks up the fresh data.
 */
@Injectable({ providedIn: 'root' })
export class MeteoRefreshService {
  private readonly http = inject(HttpClient);
  private readonly destroyRef = inject(DestroyRef);
  private readonly zarrMap = inject(ZarrMapService);

  /** ISO-8601 timestamp of the last successfully fetched meteo update. */
  readonly lastUpdated = signal<string | null>(null);

  private knownTimestamp: string | null = null;

  start(): void {
    this.poll();
    const timer = setInterval(() => this.poll(), POLL_INTERVAL_MS);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }

  private poll(): void {
    this.http.get<MeteoManifest>(MANIFEST_URL).subscribe({
      next: (manifest) => {
        const isFirstPoll = this.knownTimestamp === null;
        const isNewer = this.knownTimestamp === null || manifest.last_updated > this.knownTimestamp;
        if (isNewer) {
          this.knownTimestamp = manifest.last_updated;
          this.lastUpdated.set(manifest.last_updated);
          this.zarrMap.refreshMeteoLayers(manifest, !isFirstPoll);
          if (!isFirstPoll) {
            console.log(`[meteo] New data detected (${manifest.last_updated}), refreshing layers.`);
          }
        }
      },
      error: (err) =>
        console.warn('[meteo] Manifest poll failed (data may not be uploaded yet):', err),
    });
  }
}
