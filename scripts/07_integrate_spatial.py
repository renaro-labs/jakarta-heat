
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "processed"

grid = pd.read_csv(DATA / "grid_master.csv")
pop = pd.read_csv(DATA / "population_grid.csv")
age = pd.read_csv(DATA / "vulnerability_grid.csv")
access = pd.read_csv(DATA / "accessibility_grid.csv")

# Standarisasi nama kolom
pop = pop.rename(columns={
    "pop_sum": "population"
})

age = age.rename(columns={
    "child_sum": "pop_child",
    "elderly_sum": "pop_elderly",
    "elder;y_sum": "pop_elderly"
})

# Pastikan nama atribut sudah benar
for name, df, cols in [
    ("population", pop, ["grid_id", "population"]),
    ("vulnerability", age,
     ["grid_id", "pop_child", "pop_elderly"]),
    ("accessibility", access,
     ["grid_id", "park_distance_m"])
]:
    missing = set(cols) - set(df.columns)
    if missing:
        raise ValueError(f"{name}: missing {missing}")
    if df["grid_id"].duplicated().any():
        raise ValueError(f"{name}: duplicate grid_id")

# Integrasi berdasarkan ID
result = grid[["grid_id"]].copy()

for df in [pop, age, access]:
    result = result.merge(
        df,
        on="grid_id",
        how="left",
        validate="one_to_one"
    )

if len(result) != len(grid):
    raise ValueError("Row count mismatch")

# Validasi demografi
for col in ["population", "pop_child", "pop_elderly"]:
    if (pd.to_numeric(result[col], errors="coerce") < 0).any():
        raise ValueError(f"Negative values in {col}")

# Periksa kelengkapan
print("\nMissing values:")
print(result.isna().sum())

output = DATA / "spatial_features.csv"
result.to_csv(output, index=False)

print("\nSUCCESS!")
print("Total grids:", len(result))
print("Output:", output)
