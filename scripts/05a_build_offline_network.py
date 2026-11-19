"""Build an offline walking graph using a streaming, road-only OSM extract."""
from pathlib import Path
import argparse

from walking_network import (
    PIPELINE_VERSION, extract_walk_xml, graph_from_walk_xml, read_json,
    save_graph_atomic, save_json, source_signature, stage,
)

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/raw/osm/jakarta_all.osm")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "data/processed/walk_jakarta_all.graphml")
    parser.add_argument("--force", action="store_true", help="Rebuild extract and graph")
    args = parser.parse_args()
    if not args.source.is_file():
        parser.error(f"Local OSM XML not found: {args.source}")
    metadata = args.output.with_suffix(".meta.json")
    signature = source_signature(args.source)
    if not args.force and args.output.exists() and read_json(metadata) == signature:
        print("Full network already exists:", args.output, flush=True)
        return
    filtered = args.source.with_name(args.source.stem + "_walk.osm")
    with stage("[1/4] Streaming local OSM: keep walkable roads only"):
        extract_walk_xml(args.source, filtered, force=args.force)
    with stage("[2/4] Building and simplifying filtered walking graph"):
        graph = graph_from_walk_xml(filtered)
    import osmnx as ox
    with stage("[3/4] Projecting to EPSG:32748"):
        graph = ox.projection.project_graph(graph, to_crs="EPSG:32748")
    graph.graph["walking_pipeline_version"] = PIPELINE_VERSION
    with stage("[4/4] Saving complete network atomically"):
        save_graph_atomic(graph, args.output)
        save_json(metadata, signature)
    print(f"SUCCESS! Nodes: {len(graph):,}; edges: {graph.number_of_edges():,}", flush=True)
    print("Saved:", args.output, flush=True)


if __name__ == "__main__":
    main()
