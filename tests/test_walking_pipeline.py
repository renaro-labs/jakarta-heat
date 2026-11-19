"""Run: python -m unittest discover -s tests -v (no internet required)."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import walking_network as network
import walking_online as online

spec = importlib.util.spec_from_file_location("accessibility", SCRIPTS / "05_walking_accessibility.py")
accessibility = importlib.util.module_from_spec(spec)
spec.loader.exec_module(accessibility)

OSM = '''<osm version="0.6">
<node id="1" lat="-6.2" lon="106.8"/>
<node id="2" lat="-6.2" lon="106.801"/>
<node id="3" lat="-6.2" lon="106.802"/>
<node id="4" lat="-6.21" lon="106.81"/>
<way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/>
<tag k="highway" v="residential"/><tag k="oneway" v="yes"/>
<tag k="name" v="A &amp; B"/></way>
<way id="11"><nd ref="3"/><nd ref="4"/><tag k="building" v="yes"/></way>
<way id="12"><nd ref="3"/><nd ref="4"/><tag k="highway" v="footway"/>
<tag k="foot" v="no"/></way>
</osm>'''


class WalkingPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.osm"
        self.source.write_text(OSM, encoding="utf-8")
        self.target = self.root / "walk.osm"

    def test_pedestrian_filter(self):
        for tags in ({}, {"building": "yes"}, {"highway": ""},
                     {"highway": "motorway"}, {"highway": "footway", "foot": "no"},
                     {"highway": "residential", "access": "private"},
                     {"highway": "pedestrian", "area": "yes"}):
            with self.subTest(tags=tags):
                self.assertFalse(network.walk_allowed(tags))
        self.assertTrue(network.walk_allowed({"highway": "steps"}))
        self.assertTrue(network.walk_allowed(
            {"highway": "footway", "access": "private", "foot": "designated"}))
        self.assertEqual(network.values(" ; YES;permissive;"), {"yes", "permissive"})

    def test_streaming_extract_and_reusable_cache(self):
        network.extract_walk_xml(self.source, self.target)
        root = ET.parse(self.target).getroot()
        self.assertEqual([w.attrib["id"] for w in root.findall("way")], ["10"])
        self.assertEqual({n.attrib["id"] for n in root.findall("node")}, {"1", "2", "3"})
        self.assertEqual(root.find("way/tag[@k='name']").attrib["v"], "A & B")
        with patch.object(network, "parse_stream", side_effect=AssertionError("cache ignored")):
            network.extract_walk_xml(self.source, self.target)

    def test_incomplete_source_preserves_previous_result(self):
        network.extract_walk_xml(self.source, self.target)
        previous = self.target.read_bytes()
        self.source.write_text(OSM.replace('<nd ref="3"/>', '<nd ref="999"/>'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing"):
            network.extract_walk_xml(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), previous)
        self.assertFalse(self.target.with_name("walk.osm.partial").exists())

    def test_online_failover_and_cache(self):
        import requests
        good = Mock()
        good.json.return_value = {"elements": []}
        with patch.object(online.requests, "post", side_effect=[requests.Timeout("timeout"), good]) as post:
            path = online.fetch_tile("query", self.root, ["https://one/api", "https://two/api"],
                                     attempts=1, pause=0)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(post.call_args.kwargs["timeout"], (10, 90))
        with patch.object(online.requests, "post", side_effect=AssertionError("cache ignored")):
            self.assertEqual(online.fetch_tile("query", self.root, ["https://one/api"]), path)

    def test_online_partial_response_is_not_cached_and_retries_are_bounded(self):
        response = Mock()
        response.json.return_value = {"elements": [], "remark": "runtime error: timed out"}
        with patch.object(online.requests, "post", return_value=response) as post:
            with self.assertRaisesRegex(RuntimeError, "All Overpass attempts failed"):
                online.fetch_tile("partial", self.root, ["https://one/api", "https://two/api"],
                                  attempts=2, pause=0)
            self.assertEqual(post.call_count, 4)
        self.assertEqual(list(self.root.glob("*.json")), [])

    def test_tiles_deduplicate_and_reject_missing_nodes(self):
        payload = {"elements": [
            {"type": "node", "id": 1, "lon": 106.8, "lat": -6.2},
            {"type": "node", "id": 2, "lon": 106.801, "lat": -6.2},
            {"type": "way", "id": 10, "nodes": [1, 2], "tags": {"highway": "footway"}},
            {"type": "way", "id": 11, "nodes": [1, 2], "tags": {"building": "yes"}},
        ]}
        paths = [self.root / "tile1.json", self.root / "tile2.json"]
        for path in paths:
            path.write_text(json.dumps(payload), encoding="utf-8")
        online.tiles_to_xml(paths, self.target)
        root = ET.parse(self.target).getroot()
        self.assertEqual(len(root.findall("way")), 1)
        self.assertEqual(len(root.findall("node")), 2)
        payload["elements"] = payload["elements"][1:]
        paths[0].write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing nodes"):
            online.tiles_to_xml(paths[:1], self.root / "incomplete.osm")

    def test_graph_round_trip_and_offline_city_cache(self):
        import osmnx as ox
        from shapely.geometry import box
        network.extract_walk_xml(self.source, self.target)
        graph = network.graph_from_walk_xml(self.target)
        self.assertEqual(set(graph), {1, 3})
        self.assertTrue(graph.has_edge(1, 3))
        self.assertTrue(graph.has_edge(3, 1))
        self.assertGreater(graph[1][3][0]["length"], 200)
        graph = ox.projection.project_graph(graph, to_crs="EPSG:32748")
        graph.graph["walking_pipeline_version"] = network.PIPELINE_VERSION
        full = self.root / "walk_jakarta_all.graphml"
        network.save_graph_atomic(graph, full)
        xs = [data["x"] for _, data in graph.nodes(data=True)]
        ys = [data["y"] for _, data in graph.nodes(data=True)]
        area = box(min(xs)-100, min(ys)-100, max(xs)+100, max(ys)+100)
        with patch.object(online.requests, "post", side_effect=AssertionError("unexpected network")):
            loaded, slug = accessibility.load_network(area, "Jakarta Barat", 1500,
                                                       source="offline", data_dir=self.root)
            self.assertEqual(slug, "jakarta_barat")
            self.assertEqual(loaded.number_of_edges(), 2)
            city_file = self.root / "walk_jakarta_barat.graphml"
            with patch.object(ox.io, "load_graphml", wraps=ox.io.load_graphml) as load:
                accessibility.load_network(area, "Jakarta Barat", 1500,
                                           source="offline", data_dir=self.root)
                self.assertEqual(load.call_args.args[0], city_file)

    def test_shortest_distance_includes_park_connection_and_preserves_graph(self):
        import networkx as nx
        graph = nx.MultiDiGraph()
        graph.add_edge(1, 2, length=100)
        graph.add_edge(2, 3, length=200)
        graph.add_node(4)
        before = graph.copy()
        distances = accessibility.park_distances(graph, [3, 3], [50, 10])
        self.assertEqual(distances[1], 310)
        self.assertEqual(distances[2], 210)
        self.assertNotIn(4, distances)
        self.assertTrue(nx.utils.graphs_equal(graph, before))
        self.assertEqual(accessibility.park_distances(graph, [], []), {})

    def test_accessibility_pipeline_writes_results_without_network(self):
        import geopandas as gpd
        import osmnx as ox
        import pandas as pd
        from shapely.geometry import box
        network.extract_walk_xml(self.source, self.target)
        graph = network.graph_from_walk_xml(self.target)
        graph = ox.projection.project_graph(graph, to_crs="EPSG:32748")
        graph.graph["walking_pipeline_version"] = network.PIPELINE_VERSION
        network.save_graph_atomic(graph, self.root / "walk_jakarta_all.graphml")
        x, y = graph.nodes[1]["x"], graph.nodes[1]["y"]
        px, py = graph.nodes[3]["x"], graph.nodes[3]["y"]
        for name, attributes, geometry in [
            ("jakarta_5kota", {"kota": ["Jakarta Barat"]}, box(x-100, y-100, px+100, py+100)),
            ("grid_500m_jakarta", {"grid_id": ["TEST_1"]}, box(x-5, y-5, x+5, y+5)),
            ("parks_jakarta", {"park_id": ["PARK_1"]}, box(px-5, py-5, px+5, py+5)),
        ]:
            gpd.GeoDataFrame(attributes, geometry=[geometry], crs="EPSG:32748").to_file(
                self.root / f"{name}.gpkg", driver="GPKG", index=False)
        original_load = accessibility.load_network
        def load_local(*args, **kwargs):
            return original_load(*args, **kwargs, data_dir=self.root)
        with patch.object(accessibility, "DATA", self.root), \
             patch.object(accessibility, "load_network", side_effect=load_local), \
             patch.object(sys, "argv", ["05", "--network-source", "offline"]), \
             patch.object(online.requests, "post", side_effect=AssertionError("unexpected network")):
            accessibility.main()
        results = pd.read_csv(self.root / "accessibility_jakarta_barat.csv")
        self.assertEqual(results.loc[0, "park_reachable"], 1)
        self.assertEqual(results.loc[0, "park_access_500m"], 1)
        self.assertGreater(results.loc[0, "park_distance_m"], 200)
        self.assertEqual(len(gpd.read_file(self.root / "accessibility_jakarta_barat.gpkg")), 1)
        self.assertEqual(len(gpd.read_file(self.root / "roads_walk_jakarta_barat.gpkg")), 2)

    def test_offline_missing_network_never_calls_overpass(self):
        from shapely.geometry import box
        with patch.object(online.requests, "post", side_effect=AssertionError("unexpected network")):
            with self.assertRaisesRegex(FileNotFoundError, "05a_build_offline_network"):
                accessibility.load_network(box(0, 0, 1, 1), "Jakarta Barat", 1500,
                                           source="offline", data_dir=self.root)


if __name__ == "__main__":
    unittest.main()
