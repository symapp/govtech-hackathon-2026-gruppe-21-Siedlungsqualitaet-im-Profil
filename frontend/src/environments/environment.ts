export const environment = {
  // Temporary rollback to B2 because the new host corrupted .zarr files by uploading in ASCII transfer mode (stripping 0x0D bytes).
  // Once the files on share.unidesign.ch are re-uploaded in BINARY mode, switch back to: 'https://share.unidesign.ch/govtech'
  zarrBaseUrl: 'https://share.unidesign.ch/govtech',
  /** Set true after uploading *_500m.zarr and *_1000m.zarr from coarsen_settlement_layers.py */
  overviewCoarseAvailable: false,
  /**
   * When false, skips HTTP fetch of settlement-layer-meta.json (uses clim from config).
   * Enable once meta sidecars are deployed next to each GeoZarr store.
   */
  settlementLayerMetaAvailable: true,
};
