
from pathlib import Path
import geopandas as gpd
import osmnx as ox

ROOT = Path(__file__).resolve().parents[1]

BOUNDARY = ROOT / "data/processed/jakarta_5kota.gpkg"
OUTPUT = ROOT / "data/processed/parks_jakarta.gpkg"

ox.settings.use_cache = True

# 1. Load batas Jakarta
cities = gpd.read_file(BOUNDARY).to_crs("EPSG:4326")

# 2. Pilih Jakarta Pusat untuk uji awal
# Gabungkan batas 5 kota Jakarta
roi = cities.geometry.union_all()

# 3. Download taman dari OpenStreetMap
print("Downloading parks from OpenStreetMap...")

parks = ox.features_from_polygon(
    roi,
    tags={"leisure": "park"}
)

# 4. Simpan hanya polygon taman
parks = parks[
    parks.geometry.geom_type.isin(
        ["Polygon", "MultiPolygon"]
    )
].copy()

if parks.empty:
    raise ValueError("No park polygons found")

# 5. Identifikasi taman
parks["park_id"] = [
    f"{kind}_{osm_id}"
    for kind, osm_id in parks.index
]

if "name" not in parks.columns:
    parks["name"] = None

if "access" not in parks.columns:
    parks["access"] = None

parks["name"] = parks["name"].fillna("Unnamed Park")
parks["access"] = parks["access"].fillna("unknown")

# Kecualikan taman dengan larangan akses eksplisit.
parks = parks[
    ~parks["access"].str.lower().isin(
        ["private", "no", "customers", "permit"]
    )
].copy()

# 6. Proyeksi ke meter dan potong sesuai wilayah
parks = parks.to_crs("EPSG:32748")
cities_m = cities.to_crs("EPSG:32748")

parks = gpd.clip(
    parks, cities_m.geometry.union_all()
)
parks["area_m2"] = parks.geometry.area

# 7. Simpan hasil
OUTPUT.parent.mkdir(parents=True, exist_ok=True)

parks[
    ["park_id", "name", "access", "area_m2", "geometry"]
].to_file(OUTPUT, driver="GPKG")

print("DONE!")
print("Total park polygons:", len(parks))
print("Saved:", OUTPUT)
