"""Streaming OSM extraction and shared walking-network utilities."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
from threading import Event, Thread
from time import monotonic
import xml.etree.ElementTree as ET
from xml.sax import ContentHandler, make_parser
from xml.sax.handler import feature_external_ges
from xml.sax.xmlreader import InputSource

PIPELINE_VERSION = 2
BLOCKED = {
    "motorway", "motorway_link", "trunk", "trunk_link", "construction",
    "proposed", "planned", "abandoned", "razed", "raceway", "bus_guideway",
    "no", "platform", "rest_area", "services",
}


def values(value):
    items = value if isinstance(value, (list, tuple, set)) else [value]
    return {part.strip().lower() for item in items if item is not None
            for part in str(item).split(";") if part.strip()}


def walk_allowed(tags):
    highway = values(tags.get("highway"))
    foot = values(tags.get("foot"))
    explicit_foot = bool(foot & {"yes", "designated", "permissive"})
    if not highway or highway & BLOCKED or "yes" in values(tags.get("area")):
        return False
    if foot & {"no", "private"}:
        return False
    if values(tags.get("access")) & {"no", "private"} and not explicit_foot:
        return False
    if "private" in values(tags.get("service")) and not explicit_foot:
        return False
    if "cycleway" in highway and not explicit_foot:
        return False
    return True


@contextmanager
def stage(label):
    start = monotonic()
    stop = Event()
    print(label, flush=True)

    def heartbeat():
        while not stop.wait(15):
            print(f"  Still working: {label} ({monotonic() - start:.0f}s)", flush=True)

    worker = Thread(target=heartbeat, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join()
        print(f"  Elapsed: {monotonic() - start:.1f}s", flush=True)


class ProgressReader:
    def __init__(self, stream, size, label):
        self.stream, self.size, self.label = stream, size, label
        self.count = 0
        self.started = self.last = monotonic()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.count += len(data)
        now = monotonic()
        if now - self.last >= 10 or (not data and size != 0):
            elapsed = max(now - self.started, 0.001)
            rate = self.count / elapsed
            eta = max(0, self.size - self.count) / max(rate, 1)
            print(f"  {self.label}: {self.count / max(self.size, 1):.1%}, "
                  f"{rate / 1e6:.1f} MB/s, scan ETA {eta / 60:.1f} min", flush=True)
            self.last = now
        return data

    def close(self):
        pass  # The enclosing context owns the source file.


def parse_stream(source, handler, label):
    parser = make_parser()
    parser.setFeature(feature_external_ges, False)
    parser.setContentHandler(handler)
    with source.open("rb") as stream:
        xml_source = InputSource()
        xml_source.setByteStream(ProgressReader(stream, source.stat().st_size, label))
        parser.parse(xml_source)


def write_way(stream, way_id, nodes, tags):
    element = ET.Element("way", id=str(way_id))
    for node in nodes:
        ET.SubElement(element, "nd", ref=str(node))
    for key, value in tags.items():
        ET.SubElement(element, "tag", k=str(key), v=str(value))
    stream.write(ET.tostring(element, encoding="utf-8") + b"\n")


def write_node(stream, node_id, lon, lat, tags=None):
    element = ET.Element("node", id=str(node_id), lon=str(lon), lat=str(lat))
    for key, value in (tags or {}).items():
        ET.SubElement(element, "tag", k=str(key), v=str(value))
    stream.write(ET.tostring(element, encoding="utf-8") + b"\n")


class WayHandler(ContentHandler):
    def __init__(self, stream):
        super().__init__()
        self.stream = stream
        self.nodes_needed = set()
        self.way_id = None
        self.count = 0

    def startElement(self, name, attrs):
        if name == "way":
            self.way_id = int(attrs["id"])
            self.nodes, self.tags = [], {}
        elif self.way_id is not None:
            if name == "nd":
                self.nodes.append(int(attrs["ref"]))
            elif name == "tag":
                self.tags[attrs["k"]] = attrs["v"]

    def endElement(self, name):
        if name == "way":
            if len(self.nodes) >= 2 and walk_allowed(self.tags):
                write_way(self.stream, self.way_id, self.nodes, self.tags)
                self.nodes_needed.update(self.nodes)
                self.count += 1
            self.way_id = None


class NodeHandler(ContentHandler):
    def __init__(self, stream, nodes_needed):
        super().__init__()
        self.stream, self.nodes_needed = stream, nodes_needed
        self.node = None

    def startElement(self, name, attrs):
        if name == "node":
            node_id = int(attrs["id"])
            if node_id in self.nodes_needed:
                self.node = (node_id, attrs["lon"], attrs["lat"])
                self.tags = {}
        elif name == "tag" and self.node is not None:
            self.tags[attrs["k"]] = attrs["v"]

    def endElement(self, name):
        if name == "node" and self.node is not None:
            write_node(self.stream, *self.node, self.tags)
            self.nodes_needed.remove(self.node[0])
            self.node = None


def source_signature(source):
    stat = source.stat()
    return {"version": PIPELINE_VERSION, "path": str(source.resolve()),
            "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_json(path, value):
    temp = path.with_name(path.name + ".partial")
    temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temp.replace(path)


def extract_walk_xml(source, target, *, force=False):
    """Keep one way and referenced node IDs in RAM; spool ways to disk."""
    signature = source_signature(source)
    metadata = target.with_suffix(".meta.json")
    if not force and target.exists() and read_json(metadata) == signature:
        print(f"Using filtered OSM cache: {target}", flush=True)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    ways_path = target.with_name(target.name + ".ways.partial")
    partial = target.with_name(target.name + ".partial")
    try:
        with ways_path.open("wb") as ways:
            handler = WayHandler(ways)
            parse_stream(source, handler, "Pass 1/2: filter ways")
        if not handler.count:
            raise ValueError("No walkable ways found in the source OSM file")
        print(f"  Retained {handler.count:,} ways; "
              f"{len(handler.nodes_needed):,} referenced nodes", flush=True)
        with partial.open("wb") as output:
            output.write(b'<?xml version="1.0" encoding="UTF-8"?>\n'
                         b'<osm version="0.6" generator="jakarta-heat">\n')
            parse_stream(source, NodeHandler(output, handler.nodes_needed),
                         "Pass 2/2: extract road nodes")
            if handler.nodes_needed:
                raise ValueError(f"Source is incomplete: {len(handler.nodes_needed):,} "
                                 "referenced road nodes are missing")
            with ways_path.open("rb") as ways:
                shutil.copyfileobj(ways, output)
            output.write(b"</osm>\n")
        partial.replace(target)
        save_json(metadata, signature)
        print(f"Filtered XML: {target.stat().st_size / 1e6:.1f} MB", flush=True)
        return target
    finally:
        ways_path.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)


def configure_osmnx():
    import osmnx as ox
    ox.settings.log_console = True
    ox.settings.useful_tags_way = sorted(set(ox.settings.useful_tags_way) |
                                        {"foot", "sidewalk", "sidewalk:both"})


def graph_from_walk_xml(path, *, simplify=True):
    import osmnx as ox
    configure_osmnx()
    return ox.graph.graph_from_xml(path, bidirectional=True,
                                   simplify=simplify, retain_all=True)


def save_graph_atomic(graph, path):
    import osmnx as ox
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.stem + ".partial.graphml")
    try:
        ox.io.save_graphml(graph, filepath=partial)
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)
