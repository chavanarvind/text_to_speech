import argparse
import os
import glob
import traceback
import pandas as pd
import time
from multiprocessing import Pool, Manager, cpu_count
from azureml.core import Dataset
try:
    import sqlite3
except ImportError:
    import subprocess
    subprocess.check_call(["pip", "install", "pysqlite3-binary"])
    import sqlite3

# === Constants ===
DIRECT_MAP_KEY_1 = "Direct Mapping"
DIRECT_MAP_KEY_2 = "Direct Mapping"
JOIN_COL_1 = "CMPNT_CAT_CD_DESC"
JOIN_COL_2 = "CMPNT_MATL_TYPE_CD"
SQLITE_DB_PATH = "key_cache.sqlite"
KEY_TABLE = "keys"

# === SQLite logic ===
def init_sqlite_db():
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    cursor = conn.cursor()
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {KEY_TABLE} (
            key TEXT PRIMARY KEY
        )
    """)
    conn.commit()
    conn.close()

def insert_keys_batch(keys, lock):
    unique_keys = list(set(keys))
    values = [(k,) for k in unique_keys]
    with lock:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        cursor = conn.cursor()
        cursor.executemany(f"INSERT OR IGNORE INTO {KEY_TABLE} (key) VALUES (?)", values)
        conn.commit()
        conn.close()
    return unique_keys

# === Core logic ===
def ensure_ai_columns(df):
    for col in [
        'AI_FINAL_CATEGORY', 'AI_FINAL_CATEGORY_CONFIDENCE', 'AI_MATCHING_REASON_FINAL_CATEGORY',
        'AI_FINAL_SUBCATEGORY', 'AI_FINAL_SUBCATEGORY_CONFIDENCE', 'AI_MATCHING_REASON_FINAL_SUBCATEGORY']:
        if col not in df.columns:
            df[col] = None
    return df

def apply_existing_ai_overrides(df):
    df['skip_category_mapping'] = (
        df['AI_FINAL_CATEGORY'].notna() & df['AI_FINAL_CATEGORY'].astype(str).str.strip().ne('') &
        df['AI_FINAL_CATEGORY_CONFIDENCE'].notna() & df['AI_MATCHING_REASON_FINAL_CATEGORY'].notna())

    df['skip_subcategory_mapping'] = (
        df['AI_FINAL_SUBCATEGORY'].notna() & df['AI_FINAL_SUBCATEGORY'].astype(str).str.strip().ne('') &
        df['AI_FINAL_SUBCATEGORY_CONFIDENCE'].notna() & df['AI_MATCHING_REASON_FINAL_SUBCATEGORY'].notna())

    df.loc[df['skip_category_mapping'], 'Mapped_File_Category'] = df['AI_FINAL_CATEGORY']
    df.loc[df['skip_subcategory_mapping'], 'Mapped_File_Subcategory'] = df['AI_FINAL_SUBCATEGORY']
    return df

def clean_mapping_df(mapping_df, key_col, value_col):
    mapping_df.columns = mapping_df.columns.str.strip()
    mapping_df[key_col] = mapping_df[key_col].astype(str).str.upper().str.strip()
    mapping_df[value_col] = mapping_df[value_col].astype(str).str.strip()
    df_map = mapping_df[[key_col, value_col]].drop_duplicates()
    return df_map[df_map[value_col].notna() & df_map[value_col].ne("")]

def map_values(df, map_df1, map_df2, join_col1, join_col2, key1, key2, value_col, output_col):
    df[join_col1] = df[join_col1].astype(str).str.upper().str.strip()
    df[join_col2] = df[join_col2].astype(str).str.upper().str.strip()
    map_dict1 = dict(zip(map_df1[key1], map_df1[value_col]))
    map_dict2 = dict(zip(map_df2[key2], map_df2[value_col]))
    df[output_col] = df[join_col1].map(map_dict1)
    missing = df[output_col].isna() | df[output_col].astype(str).str.strip().eq("")
    df.loc[missing, output_col] = df.loc[missing, join_col2].map(map_dict2)
    df[output_col] = df[output_col].replace(["nan", "NaN"], pd.NA)
    return df

def add_flags(df):
    df['Mapped_File_Category Filled'] = df['Mapped_File_Category'].notna() & df['Mapped_File_Category'].astype(str).str.strip().ne("")
    df['Mapped_File_Subcategory Filled'] = df['Mapped_File_Subcategory'].notna() & df['Mapped_File_Subcategory'].astype(str).str.strip().ne("")

    df['category_matching_reason'] = df['AI_MATCHING_REASON_FINAL_CATEGORY'].where(
        df['AI_MATCHING_REASON_FINAL_CATEGORY'].notna(),
        df['Mapped_File_Category Filled'].map({True: 'direct_mapping', False: 'unmapped'}))

    df['subcategory_matching_reason'] = df['AI_MATCHING_REASON_FINAL_SUBCATEGORY'].where(
        df['AI_MATCHING_REASON_FINAL_SUBCATEGORY'].notna(),
        df['Mapped_File_Subcategory Filled'].map({True: 'direct_mapping', False: 'unmapped'}))

    df['Final Category Confidence Score'] = df['AI_FINAL_CATEGORY_CONFIDENCE'].where(
        df['AI_FINAL_CATEGORY_CONFIDENCE'].notna(),
        df['Mapped_File_Category Filled'].map({True: 1.0, False: None}))

    df['Final Subcategory Confidence Score'] = df['AI_FINAL_SUBCATEGORY_CONFIDENCE'].where(
        df['AI_FINAL_SUBCATEGORY_CONFIDENCE'].notna(),
        df['Mapped_File_Subcategory Filled'].map({True: 1.0, False: None}))

    df['needs_model'] = ~(df['Mapped_File_Category Filled'] & df['Mapped_File_Subcategory Filled'])
    df['needs_category_model'] = ~df['Mapped_File_Category Filled']
    df['needs_subcategory_model'] = ~df['Mapped_File_Subcategory Filled']
    return df

def finalize_output(df, output_path):
    df['AI_FINAL_CATEGORY'] = df['Mapped_File_Category']
    df['AI_FINAL_CATEGORY_CONFIDENCE'] = df['Final Category Confidence Score']
    df['AI_MATCHING_REASON_FINAL_CATEGORY'] = df['category_matching_reason']
    df['AI_FINAL_SUBCATEGORY'] = df['Mapped_File_Subcategory']
    df['AI_FINAL_SUBCATEGORY_CONFIDENCE'] = df['Final Subcategory Confidence Score']
    df['AI_MATCHING_REASON_FINAL_SUBCATEGORY'] = df['subcategory_matching_reason']

    for col in ['AI_FINAL_CATEGORY', 'AI_FINAL_SUBCATEGORY']:
        invalid_mask = df[col].isna() | df[col].astype(str).str.strip().isin(["", "nan", "NaN"])
        if 'CATEGORY' in col:
            df.loc[invalid_mask, 'AI_FINAL_CATEGORY_CONFIDENCE'] = None
            df.loc[invalid_mask, 'AI_MATCHING_REASON_FINAL_CATEGORY'] = None
            df.loc[invalid_mask, 'needs_category_model'] = True
        else:
            df.loc[invalid_mask, 'AI_FINAL_SUBCATEGORY_CONFIDENCE'] = None
            df.loc[invalid_mask, 'AI_MATCHING_REASON_FINAL_SUBCATEGORY'] = None
            df.loc[invalid_mask, 'needs_subcategory_model'] = True
        df.loc[invalid_mask, 'needs_model'] = True

    df.drop(columns=[
        'Mapped_File_Category', 'Final Category Confidence Score', 'category_matching_reason',
        'Mapped_File_Subcategory', 'Final Subcategory Confidence Score', 'subcategory_matching_reason'
    ], inplace=True, errors='ignore')

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    df.to_parquet(output_path, index=False)
    print(f"[✅] Final output saved to: {output_path}")

def process_single_file(args):
    file_path, mapping_df, final_output_dir, key_output_dir, seen_components, lock = args
    start_time = time.time()
    try:
        df = pd.read_parquet(file_path)
        basename = os.path.basename(file_path)

        mapping_cols = ["CMPNT_MATL_NUM", "CMPNT_MATL_DESC", "CMPNT_MATL_TYPE_CD", "CMPNT_CAT_CD_DESC", "CMPNT_UOM_CD"]
        key_cols = mapping_cols + ["LOGL_KEY_COMB_COL_VAL"]

        df_key = df[key_cols].dropna(subset=["LOGL_KEY_COMB_COL_VAL"]).drop_duplicates(subset=["LOGL_KEY_COMB_COL_VAL"])
        keys = df_key["LOGL_KEY_COMB_COL_VAL"].dropna().drop_duplicates().tolist()
        new_key_rows = insert_keys_batch(keys, lock)
        df_key = df_key[df_key["LOGL_KEY_COMB_COL_VAL"].isin(new_key_rows)]
        if not df_key.empty:
            key_output_file = os.path.join(key_output_dir, f"key_{basename}")
            df_key.to_parquet(key_output_file, index=False)

        df_mapping = df[mapping_cols].dropna(subset=["CMPNT_MATL_DESC"]).drop_duplicates(subset=["CMPNT_MATL_NUM"])
        new_ids = []
        with lock:
            for comp_id in df_mapping["CMPNT_MATL_NUM"]:
                if comp_id not in seen_components:
                    seen_components[comp_id] = True
                    new_ids.append(comp_id)
        df_mapping = df_mapping[df_mapping["CMPNT_MATL_NUM"].isin(new_ids)]

        if not df_mapping.empty:
            df_mapping = ensure_ai_columns(df_mapping)
            df_mapping = apply_existing_ai_overrides(df_mapping)

            cat_map_1 = clean_mapping_df(mapping_df, DIRECT_MAP_KEY_1, 'Mapped_File_Category')
            cat_map_2 = clean_mapping_df(mapping_df, DIRECT_MAP_KEY_2, 'Mapped_File_Category')
            df_mapping = map_values(df_mapping, cat_map_1, cat_map_2, JOIN_COL_1, JOIN_COL_2,
                                    DIRECT_MAP_KEY_1, DIRECT_MAP_KEY_2, 'Mapped_File_Category', 'Mapped_File_Category')

            sub_map_1 = clean_mapping_df(mapping_df, DIRECT_MAP_KEY_1, 'Mapped_File_Subcategory')
            sub_map_2 = clean_mapping_df(mapping_df, DIRECT_MAP_KEY_2, 'Mapped_File_Subcategory')
            df_mapping = map_values(df_mapping, sub_map_1, sub_map_2, JOIN_COL_1, JOIN_COL_2,
                                    DIRECT_MAP_KEY_1, DIRECT_MAP_KEY_2, 'Mapped_File_Subcategory', 'Mapped_File_Subcategory')

            df_mapping = add_flags(df_mapping)
            mapped_output_file = os.path.join(final_output_dir, f"mapped_{basename}")
            finalize_output(df_mapping, mapped_output_file)

        duration = time.time() - start_time
        print(f"[✅] Finished: {basename} in {duration:.2f}s ➤ Keys added: {len(new_key_rows)}, Components added: {len(new_ids)}")

    except Exception as e:
        print(f"[❌ ERROR] File: {file_path}\n{e}")
        traceback.print_exc()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_path", type=str, required=True)
    parser.add_argument("--mapping_csv", type=str, required=True)
    parser.add_argument("--key_output", type=str, required=True)
    parser.add_argument("--final_output", type=str, required=True)
    args = parser.parse_args()

    parquet_files = glob.glob(os.path.join(args.input_path, "*.parquet"))
    if not parquet_files:
        raise FileNotFoundError(f"No parquet files found in {args.input_path}")

    csv_files = glob.glob(os.path.join(args.mapping_csv, "*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No CSV mapping file found in {args.mapping_csv}")
    mapping_df = pd.read_csv(csv_files[0])

    os.makedirs(args.final_output, exist_ok=True)
    os.makedirs(args.key_output, exist_ok=True)

    init_sqlite_db()

    with Manager() as manager:
        seen_components = manager.dict()
        lock = manager.Lock()

        args_list = [
            (f, mapping_df, args.final_output, args.key_output, seen_components, lock)
            for f in parquet_files
        ]

        with Pool(cpu_count() - 2) as pool:
            for _ in pool.imap_unordered(process_single_file, args_list):
                pass

    print("\n✅ All files processed with optimized SQLite-based deduplication.")

if __name__ == "__main__":
    main()