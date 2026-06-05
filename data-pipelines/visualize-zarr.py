from pathlib import Path

import leafmap.foliumap as leafmap
import rioxarray  # noqa: F401 - needed to register the .rio accessor on xarray objects
import streamlit as st
import xarray as xr

from zarr_b2_upload import (
    BUCKET_NAME,
    credentials_configured,
    create_s3_filesystem,
    discover_zarr_stores_s3,
    s3_storage_options,
)


def discover_zarr_stores(search_root: Path) -> list[Path]:
    if not search_root.exists():
        return []

    zarr_dirs = sorted(search_root.rglob("*.zarr"))
    return [path for path in zarr_dirs if path.is_dir()]


def variable_options(ds: xr.Dataset) -> list[str]:
    return [name for name, data_var in ds.data_vars.items() if data_var.ndim >= 2]


def filter_store_paths(paths: list[str], query: str) -> list[str]:
    needle = query.strip().lower()
    if not needle:
        return paths
    return [path for path in paths if needle in path.lower()]


def open_zarr_dataset(store_path: str) -> xr.Dataset:
    if store_path.startswith("s3://"):
        return xr.open_dataset(
            store_path,
            engine="zarr",
            chunks={},
            storage_options=s3_storage_options(),
        )
    return xr.open_dataset(store_path, engine="zarr", chunks={})


@st.cache_data(ttl=300, show_spinner=False)
def preview_zarr_metadata(store_path: str) -> dict:
    ds = open_zarr_dataset(store_path)
    try:
        return {
            "variables": list(ds.data_vars),
            "dimensions": dict(ds.sizes),
            "attributes": dict(ds.attrs),
        }
    finally:
        ds.close()


def select_local_store() -> str | None:
    default_root = Path(__file__).resolve().parent
    search_root_input = st.sidebar.text_input(
        "Search root directory",
        value=str(default_root),
        help="The app scans this folder recursively for '.zarr' stores.",
    )
    search_root = Path(search_root_input).expanduser()

    if st.sidebar.button("Refresh Zarr list", key="refresh_local"):
        st.rerun()

    zarr_candidates = discover_zarr_stores(search_root)
    filtered = filter_store_paths(
        [str(path) for path in zarr_candidates],
        st.sidebar.text_input("Search", key="local_search", placeholder="Filter by name…"),
    )

    manual_path = st.sidebar.text_input(
        "Or enter a custom Zarr path",
        value="",
        help="Use this if your store is outside the search root.",
        key="local_manual",
    ).strip()

    if manual_path:
        return manual_path
    if filtered:
        return st.sidebar.selectbox(
            "Discovered Zarr stores",
            options=filtered,
            key="local_select",
        )
    st.warning("No Zarr store selected. Add one or adjust the search root.")
    if not zarr_candidates:
        st.info("No '.zarr' directories were found in the selected root.")
    elif not filtered:
        st.info("No stores match your search filter.")
    return None


def select_s3_store() -> str | None:
    if not credentials_configured():
        st.sidebar.error(
            "B2 credentials not configured. Copy `.env.example` to `.env` at the repo root."
        )
        st.stop()

    bucket = st.sidebar.text_input(
        "Bucket",
        value=BUCKET_NAME,
        help="Default from B2_BUCKET_NAME in .env",
        key="s3_bucket",
    )
    prefix = st.sidebar.text_input(
        "Prefix (optional)",
        value="",
        help="Limit listing to keys under this prefix.",
        key="s3_prefix",
    ).strip()

    if st.sidebar.button("Refresh S3 list", key="refresh_s3"):
        st.cache_data.clear()
        st.rerun()

    cache_key = f"{bucket}/{prefix}"
    if (
        "s3_zarr_list" not in st.session_state
        or st.session_state.get("s3_zarr_cache_key") != cache_key
    ):
        with st.spinner("Listing Zarr stores in S3…"):
            fs = create_s3_filesystem()
            st.session_state.s3_zarr_list = discover_zarr_stores_s3(fs, bucket, prefix)
            st.session_state.s3_zarr_cache_key = cache_key

    all_stores: list[str] = st.session_state.s3_zarr_list
    filtered = filter_store_paths(
        all_stores,
        st.sidebar.text_input(
            "Search",
            key="s3_search",
            placeholder="Filter by name…",
        ),
    )

    manual_uri = st.sidebar.text_input(
        "Or enter s3:// URI",
        value="",
        help="Example: s3://egov-hackathon/my_layer.zarr",
        key="s3_manual",
    ).strip()

    if manual_uri:
        return manual_uri if manual_uri.startswith("s3://") else f"s3://{manual_uri}"
    if filtered:
        return st.sidebar.selectbox(
            "Discovered Zarr stores",
            options=filtered,
            key="s3_select",
        )

    st.warning("No Zarr store selected. Adjust bucket/prefix or enter an s3:// URI.")
    if not all_stores:
        st.info("No '.zarr' stores were found under the given bucket/prefix.")
    elif not filtered:
        st.info("No stores match your search filter.")
    return None


st.set_page_config(page_title="Zarr Visualizer", layout="wide")
st.title("Dynamic GeoZarr Visualizer")
st.caption("Load and visualize pipeline Zarr output from local disk or S3 (local MinIO).")

data_source = st.sidebar.radio(
    "Data source",
    options=["Local files", "S3 (local MinIO)"],
    horizontal=True,
)

from_s3 = data_source.startswith("S3")
selected_path = select_s3_store() if from_s3 else select_local_store()

if not selected_path:
    st.stop()

with st.expander("Preview metadata", expanded=True):
    try:
        preview = preview_zarr_metadata(selected_path)
        st.write("**Store:**", selected_path)
        st.write("**Variables:**", preview["variables"])
        st.write("**Dimensions:**", preview["dimensions"])
        st.write("**Attributes:**", preview["attributes"])
    except Exception as err:
        st.error(f"Could not preview store: {err}")
        st.info("Ensure the path/URI points to a valid Zarr store and credentials are correct.")
        st.stop()

try:
    ds = open_zarr_dataset(selected_path)
except Exception as err:
    st.error(f"Failed to open dataset: {err}")
    st.info("Ensure the selected path points to a valid Zarr store.")
    st.stop()

vars_for_map = variable_options(ds)
if not vars_for_map:
    st.error("No plottable variables found (expected at least 2 dimensions).")
    st.write("Available variables:", list(ds.data_vars))
    st.stop()

selected_variable = st.sidebar.selectbox("Variable", options=vars_for_map)
selected_cmap = st.sidebar.selectbox(
    "Color map",
    options=["viridis", "plasma", "magma", "cividis", "terrain"],
    index=0,
)
selected_opacity = st.sidebar.slider(
    "Opacity", min_value=0.1, max_value=1.0, value=0.7, step=0.1
)

data_slice = ds[selected_variable]
if data_slice.rio.crs is None:
    data_slice = data_slice.rio.write_crs("EPSG:2056")

m = leafmap.Map(center=[46.8, 8.2], zoom=8)
m.add_raster(
    data_slice,
    layer_name=selected_variable,
    cmap=selected_cmap,
    opacity=selected_opacity,
)
m.to_streamlit(height=700)

st.subheader("Dataset details")
st.write("Store path:", selected_path)
st.write("Variables:", list(ds.data_vars))
st.write("Dimensions:", dict(ds.sizes))
st.write("Dataset attributes:", ds.attrs)
