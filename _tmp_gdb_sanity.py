import os
import shutil
import arcpy
from gdb_utils import ensure_file_gdb, is_file_gdb
base = r"c:\Users\CSOSTAD\Desktop\designatedlands_main\outputs"
path = os.path.join(base, "_gdb_validation_test.gdb")
if os.path.exists(path):
    shutil.rmtree(path, ignore_errors=True)
os.makedirs(path, exist_ok=True)
open(os.path.join(path, "dummy.txt"), "w", encoding="utf-8").write("not a gdb")
print("Before valid:", is_file_gdb(path), "Exists:", arcpy.Exists(path))
ensure_file_gdb(path, recreate_invalid=True)
print("After valid:", is_file_gdb(path), "Exists:", arcpy.Exists(path))
shutil.rmtree(path, ignore_errors=True)
for name in os.listdir(base):
    if name.startswith("_gdb_validation_test.gdb_invalid_backup_"):
        shutil.rmtree(os.path.join(base, name), ignore_errors=True)
print("Sanity check complete")
