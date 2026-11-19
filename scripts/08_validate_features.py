
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "data/processed/spatial_features.csv"

df = pd.read_csv(path)

columns = [
    "population",
    "pop_child",
    "pop_elderly",
    "park_distance_m",
    "park_reachable",
    "park_access_500m",
    "park_access_1000m",
]

print("TOTAL GRID:", len(df))

print("\nJUMLAH NILAI NOL:")
print((df[columns] == 0).sum())

print("\nJUMLAH MISSING:")
print(df[columns].isna().sum())

print("\nPOPULATION SUMMARY:")
print(df[
    ["population", "pop_child", "pop_elderly"]
].describe())

print("\nACCESSIBILITY SUMMARY:")
print(df["park_distance_m"].describe())

print("\nGRID SNAP > 250 M:")
print((df["grid_snap_m"] > 250).sum())

print("\nUNREACHABLE CONSISTENCY:")
inconsistent = (
    df["park_distance_m"].isna()
    != (df["park_reachable"] == 0)
)
print("Inconsistent rows:", inconsistent.sum())

print("\nAGE VS TOTAL POPULATION:")
age_total = df["pop_child"] + df["pop_elderly"]
print(
    "Grid with child + elderly > total:",
    (age_total > df["population"] + 1).sum()
)
