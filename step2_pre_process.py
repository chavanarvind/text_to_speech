

import argparse
import os
import glob
import re
import pandas as pd
from multiprocessing import Pool, cpu_count
from functools import partial

# === UNIT_GROUP and CMPNT_MATL_TYPE_CATEGORY mappings ===
unit_group_map = {
    'KG': 'CHM', 'KGS': 'CHM', 'KGA': 'CHM', 'KGW': 'CHM', 'G': 'CHM', 'GR': 'CHM', 'GM': 'CHM', 'MG': 'CHM',
    'LB': 'CHM', 'LBS': 'CHM', 'OZ': 'CHM', 'OZA': 'CHM', 'GW': 'CHM', 'TON': 'CHM', 'DR': 'CHM',
    'L': 'Liquid', 'LT': 'Liquid', 'ML': 'Liquid', 'CC': 'Liquid', 'CL': 'Liquid', 'CCM': 'Liquid', 'GLL': 'Liquid',
    'EA': 'Discrete', 'PC': 'Discrete', 'PCS': 'Discrete', 'Pcs': 'Discrete', 'PKT': 'Discrete', 'PK': 'Discrete',
    'PAK': 'Discrete', 'PCK': 'Discrete', 'CS': 'Discrete', 'CSE': 'Discrete', 'CT': 'Discrete', 'CA': 'Discrete',
    'ST': 'Discrete', 'GRO': 'Discrete', 'BX': 'Discrete',
    'BOT': 'Containers', 'BOTTLE': 'Containers', 'ROLL': 'Containers', 'ROL': 'Containers', 'REEL': 'Containers', 'KAR': 'Containers',
    'FT': 'Dimensional', 'YD': 'Dimensional', 'KM': 'Dimensional', 'DM': 'Dimensional', 'M': 'Dimensional',
    'M1': 'Dimensional', 'M2': 'Dimensional', 'KM2': 'Dimensional', 'YD2': 'Dimensional', 'FT3': 'Dimensional',
    'SQM': 'Dimensional', 'sqm': 'Dimensional', 'MYD': 'Dimensional', 'MI': 'Dimensional', 'SM': 'Dimensional',
    'LM': 'Dimensional', 'LF': 'Dimensional', 'MH': 'Dimensional', 'KN': 'Dimensional', 'CH': 'Dimensional',
    'TH': 'Unclassified', 'THU': 'Unclassified', 'IM': 'Unclassified', 'NOS': 'Unclassified', 'NO': 'Unclassified',
    'TS': 'Unclassified', 'KA': 'Unclassified', 'ZPC': 'Unclassified', 'ZCT': 'Unclassified', '0%': 'Unclassified',
    'KP': 'Unclassified', 'GP': 'Unclassified', 'KAI': 'Unclassified', 'SY': 'Unclassified', 'UN': 'Unclassified',
    'MU': 'Unclassified', 'UM': 'Unclassified', 'HU': 'Unclassified'
}

def map_cmpnt_type_category(val):
    if pd.isna(val): return 'OTHER'
    val_clean = str(val).strip().upper()
    erp_type_map = {
        'FERT': 'FINISHED_PRODUCT', 'HALB': 'SEMI_FINISHED', 'ROH': 'RAW_MATERIAL',
        'VERP': 'PACKAGING_MATERIAL', 'TRAD': 'TRADED_GOOD', 'ERSA': 'SUBCONTRACT_COMPONENT',
        'API': 'API', 'TPF': 'TRADE_PRODUCT', 'PACK': 'PACKAGING', 'ZHBG': 'INTERMEDIATE',
        'EPC': 'EXCIPIENT', 'EPF': 'FINISHED_PRODUCT', 'HAWA': 'TRADING_GOOD', 'ZEXI': 'EXCIPIENT',
        'ZROH': 'RAW_MATERIAL', 'SAPR': 'PACKAGING', 'IM': 'INTERMEDIATE', 'UNBW': 'NON_VALUATED',
        'IG': 'INTERMEDIATE_GOOD'
    }
    return erp_type_map.get(val_clean, 'OTHER')

# --- Clean function ---
def clean_text(text):
    text = str(text).lower()
    text = re.sub(r'[-/]', ' ', text)
    text = re.sub(r'[^A-Za-z0-9&% ]+', '', text)
    text = re.sub(r'\s*%\s*', '% ', text)
    text = re.sub(r'canada\s*\d*|can\s*\d*|ca\s*\d*|ca$|can$|ca\s', '', text)
    text = re.sub(r'(\D)(\d+)(\s*)(ml|l|gr|gm|g|ct)', r'\1 \2\3\4 ', text)
    text = re.sub(r'(\s)(spf)\s*([\d+])', r'\1\2\3', text)
    text = re.sub(r'(\d+)\s*(ml|l|gr|gm|g|ct)(?: |$)', lambda z: z.group().replace(" ", ""), text)
    text = re.sub(r'(\D)(spf\d+)', r'\1 \2 ', text)
    text = re.sub(r'\b\d{5,}\b$', '', text)
    text = re.sub(r'\b\d+\b', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return ' '.join(dict.fromkeys(text.split()))  # remove duplicate words

# --- Abbreviation Replacer ---
def expand_abbreviations(text, abbrev_map, pattern=None):
    def repl(m): return abbrev_map.get(m.group(0).lower(), m.group(0))
    if pattern is None:
        pattern = re.compile(r'\b(' + '|'.join(re.escape(k) for k in abbrev_map.keys()) + r')\b', flags=re.IGNORECASE)
    return pattern.sub(repl, text)

def process_file(file_path, abbrev_map, pattern, output_path):
    try:
        df = pd.read_parquet(file_path)

        # Identify rows to process (confidence <= 0 or missing)
        mask_process = (
            df.get('AI_FINAL_SUBCATEGORY_CONFIDENCE', 0).fillna(0) <= 0
        ) & (
            df.get('AI_FINAL_CATEGORY_CONFIDENCE', 0).fillna(0) <= 0
        )

        df_to_process = df[mask_process].copy()
        df_to_skip = df[~mask_process].copy()

        if not df_to_process.empty:
            df_to_process['CMPNT_MATL_DESC_CLEAN'] = df_to_process['CMPNT_MATL_DESC'].astype(str).map(
                lambda x: expand_abbreviations(x, abbrev_map, pattern)
            )
            df_to_process['CMPNT_MATL_DESC_CLEAN'] = df_to_process['CMPNT_MATL_DESC_CLEAN'].map(clean_text)
            df_to_process['CMPNT_MATL_DESC_LEN'] = df_to_process['CMPNT_MATL_DESC'].astype(str).str.len()
            df_to_process['UNIT_GROUP'] = df_to_process['CMPNT_UOM_CD'].fillna('').str.upper().map(unit_group_map).fillna('Unclassified')
            df_to_process['CMPNT_MATL_TYPE_CATEGORY'] = df_to_process['CMPNT_MATL_TYPE_CD'].map(map_cmpnt_type_category)

        # Merge processed and skipped rows
        df_final = pd.concat([df_to_process, df_to_skip], ignore_index=True)

        out_file = os.path.join(output_path, os.path.basename(file_path))
        df_final.to_parquet(out_file, index=False)
        print(f"✅ Processed: {os.path.basename(file_path)}")
    except Exception as e:
        print(f"❌ Failed: {os.path.basename(file_path)} -> {e}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input_path', required=True)
    parser.add_argument('--abbrev_map', required=True)
    parser.add_argument('--output_path', required=True)
    parser.add_argument('--n_jobs', type=int, default=cpu_count())
    args = parser.parse_args()

    # Load abbreviation map
    abbrev_csv = glob.glob(os.path.join(args.abbrev_map, '*.csv'))
    if not abbrev_csv:
        raise FileNotFoundError("No abbreviation map CSV found.")
    abbrev_df = pd.read_csv(abbrev_csv[0])
    abbrev_map = dict(zip(abbrev_df['Abbreviation_list'].str.lower(), abbrev_df['Abbreviation_Expension']))
    pattern = re.compile(r'\b(' + '|'.join(re.escape(k) for k in abbrev_map.keys()) + r')\b', flags=re.IGNORECASE)

    os.makedirs(args.output_path, exist_ok=True)
    parquet_files = glob.glob(os.path.join(args.input_path, '*.parquet'))

    func = partial(process_file, abbrev_map=abbrev_map, pattern=pattern, output_path=args.output_path)
    with Pool(processes=args.n_jobs) as pool:
        pool.map(func, parquet_files)

if __name__ == '__main__':
    main()