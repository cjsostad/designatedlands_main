import arcpy
gdb = r"c:\Users\CSOSTAD\Desktop\designatedlands_main\outputs\designatedlands_output.gdb"
print("GDB exists:", arcpy.Exists(gdb))
arcpy.env.workspace = gdb
fcs = arcpy.ListFeatureClasses() or []
tbls = arcpy.ListTables() or []
print("Feature classes:", len(fcs))
for name in sorted(fcs):
    if "cha" in name.lower() or "planarized" in name.lower() or "overlapping" in name.lower():
        print(name)
print("Tables:", len(tbls))
for name in sorted(tbls):
    if "cha" in name.lower() or "summary" in name.lower() or "area" in name.lower():
        print(name)
