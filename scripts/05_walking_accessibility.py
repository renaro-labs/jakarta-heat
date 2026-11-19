"""Estimate walking access to parks using cached, offline, or tiled online roads."""
from pathlib import Path
import argparse
import math

from walking_network import (
    PIPELINE_VERSION, read_json, save_graph_atomic, save_json, source_signature, stage,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/processed"
CRS = "EPSG:32748"


def load_network(area, city, buffer_m, source="auto", refresh=False,
                 timeout=90, attempts=2, tile_size=4000, endpoints=None, data_dir=DATA):
    import osmnx as ox
    from walking_network import configure_osmnx
    configure_osmnx()
    slug = city.lower().replace(" ", "_")
    city_file = data_dir / f"walk_{slug}.graphml"
    full_file = data_dir / "walk_jakarta_all.graphml"
    metadata_file = city_file.with_suffix(".meta.json")
    metadata = read_json(metadata_file)
    expected = {"version": PIPELINE_VERSION, "city": city, "buffer_m": buffer_m}
    valid = isinstance(metadata, dict) and all(metadata.get(k) == v for k, v in expected.items())
    if valid and metadata.get("source_kind") == "offline":
        valid = full_file.exists() and metadata.get("source") == source_signature(full_file)
    if source == "online" and valid:
        valid = metadata.get("source_kind") == "online"
    # Preserve legacy Jakarta Pusat caches until a new offline graph is available.
    legacy = metadata is None and not full_file.exists() and source != "online"
    if city_file.exists() and not refresh and (valid or legacy):
        print("Using city network cache:", city_file, flush=True)
        if legacy:
            print("  Legacy cache: use --refresh-network to apply the updated walking filter.", flush=True)
        graph = ox.io.load_graphml(city_file)
        if graph.number_of_edges() == 0:
            raise ValueError(f"Empty network cache: {city_file}; rerun with --refresh-network")
        if str(graph.graph["crs"]).upper() != CRS:
            graph = ox.projection.project_graph(graph, to_crs=CRS)
        return graph, slug

    if source != "online" and full_file.exists():
        print("Using full offline network:", full_file, flush=True)
        graph = ox.io.load_graphml(full_file)
        if int(graph.graph.get("walking_pipeline_version", 0)) != PIPELINE_VERSION:
            raise RuntimeError("Offline graph uses the old filter. Rebuild with "
                               "python scripts/05a_build_offline_network.py")
        if str(graph.graph["crs"]).upper() != CRS:
            graph = ox.projection.project_graph(graph, to_crs=CRS)
        source_metadata = {"source_kind": "offline", "source": source_signature(full_file)}
    elif source == "offline":
        raise FileNotFoundError(
            "Offline walking network is unavailable. Run "
            "python scripts/05a_build_offline_network.py first."
        )
    else:
        from walking_online import DEFAULT_ENDPOINTS, download_walk_graph
        print("No usable city cache selected; downloading small, resumable tiles.", flush=True)
        graph = download_walk_graph(
            area, ROOT / "cache/walking_network",
            endpoints=endpoints or DEFAULT_ENDPOINTS, timeout=timeout,
            attempts=attempts, tile_size=tile_size,
        )
        graph = ox.simplification.simplify_graph(graph)
        graph = ox.projection.project_graph(graph, to_crs=CRS)
        source_metadata = {"source_kind": "online"}

    graph = ox.truncate.truncate_graph_polygon(graph, area, truncate_by_edge=True)
    if not graph.number_of_edges():
        raise ValueError("No walking roads found inside the selected city and buffer")
    graph.graph["walking_pipeline_version"] = PIPELINE_VERSION
    save_graph_atomic(graph, city_file)
    save_json(metadata_file, {**expected, **source_metadata})
    return graph, slug


def park_distances(graph, park_nodes, park_snap):
    """Connect a virtual destination without copying the whole graph."""
    import networkx as nx
    source = object()
    costs = {}
    for node, snap in zip(park_nodes, park_snap):
        costs[node] = min(costs.get(node, math.inf), float(snap))
    if not costs:
        return {}
    try:
        for node, cost in costs.items():
            graph.add_edge(node, source, length=cost)
        return nx.single_source_dijkstra_path_length(
            graph.reverse(copy=False), source=source, weight="length"
        )
    finally:
        graph.remove_node(source)


def save_geopackage(frame, path, layer):
    partial = path.with_name(path.stem + ".partial.gpkg")
    try:
        frame.to_file(partial, layer=layer, driver="GPKG", index=False)
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default="Jakarta Barat")
    parser.add_argument("--buffer-m", type=float, default=1500)
    parser.add_argument("--network-source", choices=("auto", "offline", "online"), default="auto",
                        help="auto: city cache, then full offline graph, then online")
    parser.add_argument("--refresh-network", action="store_true", help="Ignore the city graph cache")
    parser.add_argument("--timeout", type=int, default=90, help="Read timeout per online request")
    parser.add_argument("--attempts", type=int, default=2, help="Attempts per endpoint and tile")
    parser.add_argument("--tile-size-m", type=int, default=4000)
    parser.add_argument("--overpass-url", action="append",
                        help="Override endpoints; repeat to configure failover")
    args = parser.parse_args()
    if min(args.timeout, args.attempts, args.tile_size_m) <= 0 or args.buffer_m < 0:
        parser.error("Timeout, attempts and tile size must be positive; buffer must be nonnegative")

    import geopandas as gpd
    import osmnx as ox

    print("[1/6] Loading Jakarta GIS data...", flush=True)
    cities = gpd.read_file(DATA / "jakarta_5kota.gpkg").to_crs(CRS)
    grid = gpd.read_file(DATA / "grid_500m_jakarta.gpkg").to_crs(CRS)
    parks = gpd.read_file(DATA / "parks_jakarta.gpkg").to_crs(CRS)
    city = cities[cities["kota"].astype(str).str.contains(args.city, case=False, regex=False)]
    if len(city) != 1:
        parser.error(f"Expected one city for {args.city!r}, found {len(city)}")
    city_geom = city.geometry.union_all()
    area = city_geom.buffer(args.buffer_m)
    grid = grid[grid.geometry.representative_point().within(city_geom)].copy()
    parks = parks[parks.geometry.intersects(area) &
                  parks.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
    if grid.empty or parks.empty:
        parser.error("No grids or parks found for the selected city")

    with stage("[2/6] Loading walking network"):
        graph, slug = load_network(
            area, args.city, args.buffer_m, args.network_source, args.refresh_network,
            args.timeout, args.attempts, args.tile_size_m, args.overpass_url,
        )
    print(f"Road nodes: {len(graph):,}; edges: {graph.number_of_edges():,}", flush=True)

    with stage("[3/6] Exporting roads to GeoPackage"):
        roads = ox.convert.graph_to_gdfs(graph, nodes=False)
        roads_file = DATA / f"roads_walk_{slug}.gpkg"
        save_geopackage(roads[["geometry"]], roads_file, "roads")
        del roads

    with stage("[4/6] Connecting grid and park points to the network"):
        grid_pts = grid.geometry.representative_point()
        park_pts = parks.geometry.representative_point()
        grid_nodes, grid_snap = ox.distance.nearest_nodes(
            graph, X=grid_pts.x.to_numpy(), Y=grid_pts.y.to_numpy(), return_dist=True,
        )
        park_nodes, park_snap = ox.distance.nearest_nodes(
            graph, X=park_pts.x.to_numpy(), Y=park_pts.y.to_numpy(), return_dist=True,
        )
    with stage("[5/6] Calculating shortest walking distances"):
        distances = park_distances(graph, park_nodes, park_snap)
        grid["park_distance_m"] = [
            distances.get(node, math.nan) + float(snap)
            for node, snap in zip(grid_nodes, grid_snap)
        ]
    grid["grid_snap_m"] = grid_snap
    grid["park_reachable"] = grid["park_distance_m"].notna().astype(int)
    grid["park_access_500m"] = (grid["park_distance_m"] <= 500).astype(int)
    grid["park_access_1000m"] = (grid["park_distance_m"] <= 1000).astype(int)

    with stage("[6/6] Saving results"):
        columns = ["grid_id", "park_distance_m", "grid_snap_m", "park_reachable",
                   "park_access_500m", "park_access_1000m"]
        csv_file = DATA / f"accessibility_{slug}.csv"
        partial = csv_file.with_suffix(".partial.csv")
        grid[columns].to_csv(partial, index=False)
        partial.replace(csv_file)
        save_geopackage(grid[columns + ["geometry"]],
                        DATA / f"accessibility_{slug}.gpkg", "accessibility")

    print(f"SUCCESS! City: {args.city}; grids: {len(grid)}; parks: {len(parks)}", flush=True)
    print("Unreachable:", int(grid["park_distance_m"].isna().sum()), flush=True)
    print("CSV:", csv_file, flush=True)


if __name__ == "__main__":
    main()
