"""Bounded Overpass requests with resumable per-tile caching."""
import hashlib
import math
from pathlib import Path
import time

import requests

from walking_network import (
    graph_from_walk_xml, read_json, save_json, walk_allowed, write_node, write_way,
)

DEFAULT_ENDPOINTS = (
    "https://overpass-api.de/api",
    "https://overpass.private.coffee/api",
)


def validate_response(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ValueError("Overpass returned invalid JSON: missing elements list")
    if payload.get("remark"):
        raise ValueError(f"Overpass returned an incomplete result: {payload['remark']}")
    return payload


def fetch_tile(query, cache_dir, endpoints, *, timeout=90, attempts=2, pause=5):
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / (hashlib.sha256(query.encode()).hexdigest() + ".json")
    cached = read_json(cache_file)
    if cached is not None:
        try:
            validate_response(cached)
            print(f"  Reusing tile {cache_file.stem[:10]}", flush=True)
            return cache_file
        except ValueError:
            print("  Invalid tile cache: downloading again", flush=True)
    errors = []
    for attempt in range(attempts):
        for endpoint in endpoints:
            endpoint = endpoint.rstrip("/").removesuffix("/interpreter")
            print(f"  Request {attempt + 1}/{attempts}: {endpoint}", flush=True)
            try:
                response = requests.post(
                    endpoint + "/interpreter", data={"data": query},
                    timeout=(10, timeout),
                    headers={"User-Agent": "jakarta-heat walking-accessibility/2"},
                )
                response.raise_for_status()
                payload = validate_response(response.json())
                save_json(cache_file, payload)
                return cache_file
            except (requests.RequestException, ValueError) as error:
                errors.append(f"{endpoint}: {error}")
                print(f"  Request failed: {error}", flush=True)
                time.sleep(min(pause * (attempt + 1), 30))
    raise RuntimeError(
        "All Overpass attempts failed. Completed tiles are cached; rerun to resume. "
        "For fully offline use, run scripts/05a_build_offline_network.py first.\n"
        + "\n".join(errors)
    )


def tile_queries(area, *, tile_size=4000, timeout=90):
    """Yield small WGS84 bbox queries covering an EPSG:32748 area."""
    import geopandas as gpd
    from shapely.geometry import box
    minx, miny, maxx, maxy = area.bounds
    for row in range(math.ceil((maxy - miny) / tile_size)):
        for col in range(math.ceil((maxx - minx) / tile_size)):
            cell = box(minx + col * tile_size, miny + row * tile_size,
                       min(minx + (col + 1) * tile_size, maxx),
                       min(miny + (row + 1) * tile_size, maxy))
            if not cell.intersects(area):
                continue
            west, south, east, north = gpd.GeoSeries(
                [cell], crs="EPSG:32748"
            ).to_crs("EPSG:4326").total_bounds
            # Fetch all highway ways; apply the shared pedestrian filter locally.
            # Recursion retrieves every referenced node, including outside the bbox.
            yield (f'[out:json][timeout:{max(1, timeout - 10)}];'
                   f'(way["highway"]({south:.7f},{west:.7f},{north:.7f},{east:.7f});>;);'
                   'out body qt;')


def tiles_to_xml(tile_paths, target):
    """Deduplicate ways across tiles and write only nodes referenced by roads."""
    ways = {}
    needed = set()
    for path in tile_paths:
        payload = validate_response(read_json(path))
        for element in payload["elements"]:
            if element.get("type") != "way":
                continue
            tags = element.get("tags", {})
            nodes = element.get("nodes", [])
            if len(nodes) >= 2 and walk_allowed(tags):
                ways[element["id"]] = (nodes, tags)
                needed.update(nodes)
    if not ways:
        raise ValueError("No walkable roads found in downloaded tiles")
    partial = target.with_name(target.name + ".partial")
    try:
        with partial.open("wb") as output:
            output.write(b'<osm version="0.6" generator="jakarta-heat">\n')
            for path in tile_paths:
                payload = validate_response(read_json(path))
                for element in payload["elements"]:
                    if element.get("type") == "node" and element["id"] in needed:
                        write_node(output, element["id"], element["lon"], element["lat"],
                                   element.get("tags"))
                        needed.remove(element["id"])
            if needed:
                raise ValueError(f"Downloaded network is incomplete: {len(needed)} missing nodes")
            for way_id, (nodes, tags) in ways.items():
                write_way(output, way_id, nodes, tags)
            output.write(b"</osm>\n")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def download_walk_graph(area, cache_dir, *, endpoints=DEFAULT_ENDPOINTS,
                        timeout=90, attempts=2, tile_size=4000):
    queries = list(tile_queries(area, tile_size=tile_size, timeout=timeout))
    tiles = []
    for index, query in enumerate(queries, 1):
        print(f"Online tile {index}/{len(queries)}", flush=True)
        tiles.append(fetch_tile(query, cache_dir, endpoints,
                                timeout=timeout, attempts=attempts))
        if index < len(queries):
            time.sleep(1)
    # A unique name allows different city extracts to coexist.
    key = hashlib.sha256("".join(queries).encode()).hexdigest()[:16]
    extract = tiles_to_xml(tiles, cache_dir / f"walk_{key}.osm")
    return graph_from_walk_xml(extract, simplify=False)
