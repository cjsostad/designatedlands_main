import arcpy
import os
from collections import Counter


def consolidate_slivers(
    input_fc,
    final_gdb,
    working_folder,
    output_name,
):
    """Dissolve small sliver polygons from a planarized CHA intersection."""

    print("Sliver consolidator module:", os.path.abspath(__file__))

    # ============================================================
    # 1. INPUT / OUTPUT
    # ============================================================

    os.makedirs(os.path.dirname(final_gdb), exist_ok=True)
    os.makedirs(working_folder, exist_ok=True)
    temp_gdb = os.path.join(working_folder, "Dissolve_Work.gdb")
    output_fc = os.path.join(final_gdb, output_name)
    # Create geodatabases if needed
    for gdb_path, label in [(final_gdb, "Final GDB"), (temp_gdb, "Temp GDB")]:
        if not arcpy.Exists(gdb_path):
            arcpy.management.CreateFileGDB(
                out_folder_path=os.path.dirname(gdb_path),
                out_name=os.path.basename(gdb_path)
            )
            print(f"{label} created: {gdb_path}")
        else:
            print(f"{label} already exists: {gdb_path}")

    print("Input:", input_fc)
    print("Final output:", output_fc)
    print("Temporary GDB:", temp_gdb)


    # ============================================================
    # 2. FIELD DEFINITIONS
    # ============================================================

    group_fields = [
        "CommName_E",
        "CHA_Source_ID",
    ]

    priority_fields = [
        "designation",
        "source_id",
        "source_name",
        "forest_restriction_max",
        "mine_restriction_max",
        "og_restriction_max",
        "SiteName_E",
        "ProvTerr_E",
        "MigBird",
        "Taxon",
        "UTM_Zone",
        "Easting",
        "Northing",
        "Latitude",
        "Longitude",
        "CH_Format",
        "Type_E",

    ]

    mode_fields = [
        "Sub_Species_E",
        "SARA_Status",
        "SiteID"
    ]

    mutual_fields = [
        "CommName_E",
        "CHA_Source_ID",
        "SciName",
        "Population_E"
    ]

    first_fields = [
        "FID_designations_planarized"
    ]

    sum_fields = [
        "Overlap_Area_Ha",
        "ECCC_Cha_Area_Ha",
        "Cha_Area_Fed_Removed"
    ]


    # ============================================================
    # 3. VALIDATE INPUT FIELDS
    # ============================================================

    print("\nStep 1: Validating fields...")

    actual_fields = {field.name for field in arcpy.ListFields(input_fc)}

    required_fields = group_fields + ["Shape_Area"]

    missing_fields = [field for field in required_fields if field not in actual_fields]

    if missing_fields:
        print("\nERROR - Missing fields:")
        for field in missing_fields:
            print("   ", field)
        raise RuntimeError("One or more required fields were not found.")

    optional_field_groups = [
        ("priority", priority_fields),
        ("mode", mode_fields),
        ("mutual", mutual_fields),
        ("first-value", first_fields),
        ("sum", sum_fields),
    ]
    for label, fields in optional_field_groups:
        missing_optional = [field for field in fields if field not in actual_fields]
        if missing_optional:
            print(
                f"WARNING - Skipping missing {label} fields:",
                ", ".join(missing_optional),
            )
        fields[:] = [field for field in fields if field in actual_fields]

    optional_cursor_fields = ["process_order", "overlapping_designations"]
    missing_cursor_fields = [
        field for field in optional_cursor_fields if field not in actual_fields
    ]
    if missing_cursor_fields:
        print(
            "WARNING - Skipping missing attribute fields:",
            ", ".join(missing_cursor_fields),
        )
    optional_cursor_fields = [
        field for field in optional_cursor_fields if field in actual_fields
    ]

    print("All required fields were found.")


    # ============================================================
    # 4. CREATE FILTERED AND NON-FILTERED DATASETS
    # ============================================================

    print("\nStep 2: Selecting polygons with Shape_Area < 1000...")

    filtered_fc = os.path.join(temp_gdb, "Filtered_Small")
    non_filtered_fc = os.path.join(temp_gdb, "Non_Filtered")

    for fc in [filtered_fc, non_filtered_fc]:
        if arcpy.Exists(fc):
            arcpy.management.Delete(fc)

    layer_name = "input_layer"

    if arcpy.Exists(layer_name):
        arcpy.management.Delete(layer_name)

    arcpy.management.MakeFeatureLayer(input_fc, layer_name)

    where_clause = '"Shape_Area" < 1000'
    arcpy.management.SelectLayerByAttribute(layer_name, "NEW_SELECTION", where_clause)

    small_count = int(arcpy.management.GetCount(layer_name)[0])
    print("Small polygons:", small_count)

    arcpy.management.CopyFeatures(layer_name, filtered_fc)

    arcpy.management.SelectLayerByAttribute(layer_name, "SWITCH_SELECTION")
    non_small_count = int(arcpy.management.GetCount(layer_name)[0])
    print("Non-small polygons:", non_small_count)

    arcpy.management.CopyFeatures(layer_name, non_filtered_fc)


    # ============================================================
    # 5. DISSOLVE SMALL POLYGONS
    # ============================================================

    print("\nStep 3: Dissolving small polygons...")

    dissolved_fc = os.path.join(temp_gdb, "Dissolved_Small")

    if arcpy.Exists(dissolved_fc):
        arcpy.management.Delete(dissolved_fc)

    print("Dissolve input:", filtered_fc)
    print("Dissolve fields:", group_fields)

    available_fields = [f.name for f in arcpy.ListFields(filtered_fc)]
    for field in group_fields:
        print(field, "→", field in available_fields)

    arcpy.management.Dissolve(
        in_features=filtered_fc,
        out_feature_class=dissolved_fc,
        dissolve_field=group_fields,
        multi_part="MULTI_PART"
    )

    dissolved_count = int(arcpy.management.GetCount(dissolved_fc)[0])
    print("Dissolved groups:", dissolved_count)


    # ============================================================
    # 6. READ ATTRIBUTES FROM SMALL POLYGONS
    # ============================================================

    print("\nStep 4: Reading attributes...")

    attribute_fields = list(dict.fromkeys(
        group_fields
        + optional_cursor_fields
        + priority_fields
        + mode_fields
        + mutual_fields
        + first_fields
        + sum_fields
    ))

    records_by_group = {}

    with arcpy.da.SearchCursor(filtered_fc, attribute_fields) as cursor:
        for row in cursor:
            record = dict(zip(attribute_fields, row))
            group_key = tuple(record[field] for field in group_fields)

            if group_key not in records_by_group:
                records_by_group[group_key] = []

            records_by_group[group_key].append(record)

    print("Attribute groups:", len(records_by_group))


    # ============================================================
    # 7. HELPER FUNCTIONS
    # ============================================================

    def get_priority_record(records):
        valid_records = [r for r in records if r.get("process_order") is not None]
        if not valid_records:
            return records[0]
        return min(valid_records, key=lambda r: r["process_order"])

    def get_mode(records, field):
        values = [r.get(field) for r in records if r.get(field) is not None]
        if not values:
            return None
        return Counter(values).most_common(1)[0][0]

    def get_first(records, field):
        for record in records:
            value = record.get(field)
            if value is not None:
                return value
        return None

    def get_sum(records, field):
        values = [r.get(field) for r in records if r.get(field) is not None]
        if not values:
            return None
        return sum(values)


    # ============================================================
    # 8. BUILD DISSOLVED ATTRIBUTES
    # ============================================================

    print("\nStep 5: Calculating dissolved attributes...")

    dissolved_attributes = {}

    for group_key, records in records_by_group.items():
        attributes = {}

        # Group fields
        for field in group_fields:
            attributes[field] = records[0].get(field)

        # Highest-priority record
        priority_record = get_priority_record(records)
        attributes["process_order"] = priority_record.get("process_order")

        for field in priority_fields:
            attributes[field] = priority_record.get(field)

        attributes["overlapping_designations"] = priority_record.get("overlapping_designations")

        # Most common fields
        for field in mode_fields:
            attributes[field] = get_mode(records, field)

        # Mutual fields
        for field in mutual_fields:
            attributes[field] = records[0].get(field)

        # First fields
        for field in first_fields:
            attributes[field] = get_first(records, field)

        # Sum fields
        for field in sum_fields:
            attributes[field] = get_sum(records, field)

        # Percentage field using new name
        total_area = attributes.get("ECCC_Cha_Area_Ha")
        total_overlap = attributes.get("Overlap_Area_Ha")

        if total_area is not None and total_area != 0 and total_overlap is not None:
            attributes["Pct_of_Cha_Prot_by_LandDes"] = (total_overlap / total_area) * 100
        else:
            attributes["Pct_of_Cha_Prot_by_LandDes"] = None

        dissolved_attributes[group_key] = attributes

    print("Calculated attributes for", len(dissolved_attributes), "groups.")


    # ============================================================
    # 9. CREATE FINAL OUTPUT
    # ============================================================

    print("\nStep 6: Creating final output...")

    if arcpy.Exists(output_fc):
        print("Deleting existing output...")
        arcpy.management.Delete(output_fc)

    result = arcpy.management.CopyFeatures(
        in_features=non_filtered_fc,
        out_feature_class=output_fc
    )

    output_fc = result.getOutput(0)
    print("\nFinal output created:", output_fc)
    print("Exists:", arcpy.Exists(output_fc))


    # ============================================================
    # 10. INSERT FIELDS
    # ============================================================

    insert_fields = [
        "SHAPE@",
        "FID_designations_planarized",
        "process_order",
        "designation",
        "overlapping_designations",
        "source_id",
        "source_name",
        "forest_restriction_max",
        "mine_restriction_max",
        "og_restriction_max",
        "COSEWIC_ID",
        "SciName",
        "CommName_E",
        "Population_E",
        "Sub_Species_E",
        "SARA_Status",
        "SARA_Agency",
        "SiteID",
        "SiteName_E",
        "ProvTerr_E",
        "MigBird",
        "Taxon",
        "UTM_Zone",
        "Easting",
        "Northing",
        "Latitude",
        "Longitude",
        "GridID",
        "CH_Format",
        "Type_E",
        "Sensitive_E",
        "RDoc_Name_E",
        "RD_Status",
        "ECCC_Cha_Area_Ha",
        "CHA_Source_ID",
        "Cha_Area_Fed_Removed",
        "Overlap_Area_Ha",
        "Pct_of_Cha_Prot_by_LandDes"
    ]


    # ============================================================
    # 11. ADD DISSOLVED RECORDS
    # ============================================================

    print("\nStep 7: Adding dissolved records...")

    final_fields = {f.name for f in arcpy.ListFields(output_fc)}
    fields_to_insert = [
        field for field in insert_fields
        if field != "FID_critical_habitat_area"
        and (field == "SHAPE@" or field in final_fields)
    ]
    missing_output_fields = [
        field for field in insert_fields
        if field != "SHAPE@" and field not in final_fields
    ]

    if missing_output_fields:
        print(
            "WARNING - Skipping fields not present in the output schema:",
            ", ".join(missing_output_fields),
        )
    print("CHA_Source_ID exists:", "CHA_Source_ID" in final_fields)
    print("Number of output fields:", len(final_fields))
    print(
        "FID_critical_habitat_area passed to insert cursor:",
        "FID_critical_habitat_area" in fields_to_insert,
    )

    with arcpy.da.SearchCursor(dissolved_fc, ["SHAPE@"] + group_fields) as geometry_cursor:
        with arcpy.da.InsertCursor(output_fc, fields_to_insert) as insert_cursor:
            inserted_count = 0

            for row in geometry_cursor:
                geometry = row[0]
                group_key = tuple(row[1:])

                attributes = dissolved_attributes[group_key]

                output_row = [
                    geometry if field == "SHAPE@" else attributes.get(field)
                    for field in fields_to_insert
                ]

                insert_cursor.insertRow(output_row)
                inserted_count += 1

    print("Dissolved records inserted:", inserted_count)


    # ============================================================
    # 12. FINAL VALIDATION
    # ============================================================

    print("\nStep 8: Final validation...")

    final_count = int(arcpy.management.GetCount(output_fc)[0])
    expected_count = non_small_count + dissolved_count

    print("Non-filtered records:", non_small_count)
    print("Dissolved records:", dissolved_count)
    print("Expected final count:", expected_count)
    print("Actual final count:", final_count)

    if final_count == expected_count:
        print("\nSUCCESS!")
    else:
        print("\nWARNING: Record count does not match.")

    print("\nFinal output:", output_fc)


    # ============================================================
    # 13. COMPLETE
    # ============================================================

    print("\n==========================================")
    print("PROCESSING COMPLETE")
    print("==========================================")
    print("Output:", output_fc)
    print("Final records:", final_count)
