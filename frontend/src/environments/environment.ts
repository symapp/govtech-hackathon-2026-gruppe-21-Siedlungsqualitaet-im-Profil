export const environment = {
  // Local MinIO bucket used by data pipelines and frontend in local dev.
  zarrBaseUrl: 'http://127.0.0.1:9000/egov-hackathon',
  /** Set true after uploading *_500m.zarr and *_1000m.zarr from coarsen_settlement_layers.py */
  overviewCoarseAvailable: false,
  /**
   * When false, skips HTTP fetch of settlement-layer-meta.json (uses clim from config).
   * Enable once meta sidecars are deployed next to each GeoZarr store.
   */
  settlementLayerMetaAvailable: true,
};
