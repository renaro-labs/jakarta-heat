
from pathlib import Path
import geopandas as gpd
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/processed"

grid = gpd.read_file(DATA / "grid_500m_jakarta.gpkg")
features = pd.read_csv(DATA / "spatial_features.csv")

# Gabungkan data dengan geometri grid
gdf = grid[["grid_id", "geometry"]].merge(
    features,
    on="grid_id",
    how="left",
    validate="one_to_one"
)

# Buat indikator kualitas
gdf["qa_unreachable"] = (
    gdf["park_distance_m"].isna()
).astype(int)

gdf["qa_far_snap"] = (
    gdf["grid_snap_m"] > 250
).astype(int)

gdf["qa_age_zero"] = (
    (gdf["population"] > 0)
    & (gdf["pop_child"] == 0)
    & (gdf["pop_elderly"] == 0)
).astype(int)

# Tandai grid yang perlu diperiksa
problem = gdf[
    (gdf["qa_unreachable"] == 1)
    | (gdf["qa_far_snap"] == 1)
    | (gdf["qa_age_zero"] == 1)
].copy()

problem.to_file(
    DATA / "qa_problem_grids.gpkg",
    driver="GPKG"
)

# Simpan laporan
problem.drop(columns="geometry").to_csv(
    DATA / "qa_problem_grids.csv",
    index=False
)

print("Unreachable:", gdf["qa_unreachable"].sum())
print("Far snap:", gdf["qa_far_snap"].sum())
print("Age zero anomaly:", gdf["qa_age_zero"].sum())
print("Unique flagged grids:", len(problem))
print("QA files saved!")
