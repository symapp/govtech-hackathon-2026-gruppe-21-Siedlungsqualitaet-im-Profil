export const environment = {
  // Temporary rollback to B2 because the new host corrupted .zarr files by uploading in ASCII transfer mode (stripping 0x0D bytes).
  // Once the files on share.unidesign.ch are re-uploaded in BINARY mode, switch back to: 'https://share.unidesign.ch/govtech'
  zarrBaseUrl: 'https://share.unidesign.ch/govtech',
  overviewCoarseAvailable: false,
  settlementLayerMetaAvailable: true,
};
