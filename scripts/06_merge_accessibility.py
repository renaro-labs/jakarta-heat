
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "processed"

cities = [
    "jakarta_pusat",
    "jakarta_barat",
    "jakarta_timur",
    "jakarta_selatan",
    "jakarta_utara",
]

required = [
    "grid_id",
    "park_distance_m",
    "grid_snap_m",
    "park_reachable",
    "park_access_500m",
    "park_access_1000m",
]

frames = []

for city in cities:
    path = DATA / f"accessibility_{city}.csv"
    df = pd.read_csv(path)

    missing_columns = set(required) - set(df.columns)
    if missing_columns:
        raise ValueError(
            f"{city}: missing columns {missing_columns}"
        )

    if df["grid_id"].duplicated().any():
        raise ValueError(f"{city}: duplicate grid_id")

    print(f"{city}: {len(df)} grids")
    frames.append(df[required])

# Merge seluruh kota
result = pd.concat(frames, ignore_index=True)

if result["grid_id"].duplicated().any():
    raise ValueError("Duplicate grid_id across cities!")

# Validasi terhadap master grid
master = pd.read_csv(DATA / "grid_master.csv")

missing = set(master["grid_id"]) - set(result["grid_id"])
extra = set(result["grid_id"]) - set(master["grid_id"])

print("Missing grids:", len(missing))
print("Extra grids:", len(extra))

if missing or extra:
    raise ValueError(
        f"Grid mismatch! Missing: {list(missing)[:5]}, "
        f"Extra: {list(extra)[:5]}"
    )

# Pertahankan urutan master grid
result = master[["grid_id"]].merge(
    result,
    on="grid_id",
    how="left",
    validate="one_to_one"
)

# Validasi jarak
if (result["park_distance_m"].dropna() < 0).any():
    raise ValueError("Negative distances detected!")

print("Unreachable grids:",
      result["park_distance_m"].isna().sum())

print("Grids with snap > 250m:",
      (result["grid_snap_m"] > 250).sum())

print("Access within 500m:",
      result["park_access_500m"].sum())

# Export
output = DATA / "accessibility_grid.csv"
result.to_csv(output, index=False)

print("\nSUCCESS!")
print("Total grids:", len(result))
print("Saved:", output)
