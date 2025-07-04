#step_4_merge_with_key_files.py
import os
import argparse
import pandas as pd
import psutil
from datetime import datetime
from azureml.core import Run

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

AI_OUTPUT_COLUMNS = [
    "AI_FINAL_CATEGORY",
    "AI_FINAL_CATEGORY_CONFIDENCE",
    "AI_MATCHING_REASON_FINAL_CATEGORY",
    "AI_FINAL_SUBCATEGORY",
    "AI_FINAL_SUBCATEGORY_CONFIDENCE",
    "AI_MATCHING_REASON_FINAL_SUBCATEGORY"
]

def main(inference_output_dir, key_output_dir, final_output_dir):
    run = Run.get_context()
    os.makedirs(final_output_dir, exist_ok=True)

    all_dfs = []

    log("📁 Scanning inference output directory...")
    for file in os.listdir(inference_output_dir):
        if not file.endswith(".parquet"):
            continue
        try:
            file_path = os.path.join(inference_output_dir, file)
            df = pd.read_parquet(file_path)

            # Ensure required columns exist
            selected_cols = ["CMPNT_MATL_NUM"] + AI_OUTPUT_COLUMNS
            missing_cols = [col for col in selected_cols if col not in df.columns]
            if missing_cols:
                log(f"⚠️ Skipping {file} due to missing columns: {missing_cols}")
                continue

            df = df[selected_cols]
            df = df[df["CMPNT_MATL_NUM"].notnull()]
            all_dfs.append(df)

            log(f"✅ Loaded {file} (rows: {len(df)})")
        except Exception as e:
            log(f"❌ Failed to process file: {file} -> {e}")

    if not all_dfs:
        log("❌ No valid inference files found. Exiting.")
        return

    log("🧩 Concatenating and deduplicating predictions...")
    global_df = pd.concat(all_dfs, ignore_index=True)

    global_df = global_df.sort_values(
        by=["CMPNT_MATL_NUM", "AI_FINAL_CATEGORY_CONFIDENCE", "AI_FINAL_SUBCATEGORY_CONFIDENCE"],
        ascending=[True, False, False]
    )
    global_df = global_df.drop_duplicates(subset="CMPNT_MATL_NUM", keep="first")

    # Save combined global prediction table
    global_df_path = os.path.join(final_output_dir, "global_predictions.parquet")
    global_df.to_parquet(global_df_path, index=False)
    log(f"✅ Saved global prediction table to: {global_df_path}")

    # Merge with each key file
    for file in os.listdir(key_output_dir):
        if not file.endswith(".parquet"):
            continue

        try:
            log(f"📂 Processing key file: {file}")
            key_path = os.path.join(key_output_dir, file)
            key_df = pd.read_parquet(key_path).drop_duplicates()

            merged_df = key_df.merge(global_df, how="left", on="CMPNT_MATL_NUM")
            merged_df["File_Name"] = file
            log(f"🧾 Columns after merge: {list(merged_df.columns)}")
            # Clear AI columns for nulls
            merged_df.loc[merged_df["CMPNT_MATL_NUM"].isnull(), AI_OUTPUT_COLUMNS] = None

            out_path = os.path.join(final_output_dir, file)
            merged_df.to_parquet(out_path, index=False)

            log(f"✅ Merged output saved: {out_path} (rows: {len(merged_df)})")
            log(f"💾 Memory usage: {psutil.virtual_memory().percent}%")

        except Exception as e:
            log(f"❌ Failed to process key file: {file} -> {e}")

    # Cleanup intermediate file
    if os.path.exists(global_df_path):
        os.remove(global_df_path)
        log(f"🗑️ Deleted intermediate file: {global_df_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inference_output_dir", required=True)
    parser.add_argument("--key_output_dir", required=True)
    parser.add_argument("--final_output_dir", required=True)
    args = parser.parse_args()

    main(args.inference_output_dir, args.key_output_dir, args.final_output_dir)