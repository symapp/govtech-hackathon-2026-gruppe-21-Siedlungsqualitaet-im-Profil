import { bootstrapApplication } from '@angular/platform-browser';
import { appConfig } from './app/app.config';
import { App } from './app/app';
import { registry } from 'zarrita';
import ZstdCodec from 'numcodecs/zstd';

// Pre-register zstd codec so Angular bundles it and it's ready before any
// Zarr store with zstd-compressed chunks is opened.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
registry.set('zstd', () => Promise.resolve(ZstdCodec as any));

bootstrapApplication(App, appConfig).catch((err) => console.error(err));
