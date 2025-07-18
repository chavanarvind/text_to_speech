#step3_inference_run.py
import subprocess
import os
import argparse
import pandas as pd
import numpy as np
import torch
import joblib
import gc
from datetime import datetime
from sentence_transformers import SentenceTransformer
from azureml.core import Run, Model

subprocess.run(["pip", "install", "lightgbm"], check=True)

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def encode_descriptions(encoder, texts, batch_size=64):
    return encoder.encode(texts, batch_size=batch_size, show_progress_bar=True, convert_to_numpy=True)

def round_datetime_columns(df):
    for col in df.select_dtypes(include=["datetime64[ns]"]).columns:
        df[col] = df[col].dt.round("us")
    return df

def main(input_path, final_output_dir):
    run = Run.get_context()
    ws = run.experiment.workspace

    os.makedirs(final_output_dir, exist_ok=True)
    log_dir = os.path.join(final_output_dir, "log")
    os.makedirs(log_dir, exist_ok=True)

    log("🔁 Loading category model and encoders...")
    model_dir = Model.get_model_path("RPM_Category_model_full_cat", _workspace=ws)
    model = joblib.load(os.path.join(model_dir, "final_model.joblib"))
    ordinal = joblib.load(os.path.join(model_dir, "ordinal_encoder.pkl"))
    scaler = joblib.load(os.path.join(model_dir, "scaler.pkl"))

    encoder = SentenceTransformer(
        'pritamdeka/BioBERT-mnli-snli-scinli-scitail-mednli-stsb',
        device='cuda' if torch.cuda.is_available() else 'cpu'
    )
    encoder.max_seq_length = 128

    log("📦 Loading subcategory models...")
    subcategory_models = {}
    for cat in ['CHM', 'PKG', 'FNW']:
        model_path = Model.get_model_path(f"RPM_Category_model_full_{cat}_V1", _workspace=ws)
        subcategory_models[cat] = {
            "model": joblib.load(os.path.join(model_path, "final_model.joblib")),
            "encoder": joblib.load(os.path.join(model_path, "ordinal_encoder.pkl")),
            "scaler": joblib.load(os.path.join(model_path, "scaler.pkl")),
        }

    for f in os.listdir(input_path):
        if not f.endswith(".parquet"):
            continue

        log(f"🔎 Processing file: {f}")
        df = pd.read_parquet(os.path.join(input_path, f))
        df = round_datetime_columns(df)

        # --- Predict Category ---
        category_idx = df['needs_category_model'] == True
        df_needs_category = df[category_idx].copy()

        if not df_needs_category.empty:
            descs = df_needs_category['CMPNT_MATL_DESC_CLEAN'].astype(str).tolist()
            embeddings = encode_descriptions(encoder, descs)

            X = np.hstack([
                embeddings,
                scaler.transform(df_needs_category[['CMPNT_MATL_DESC_LEN']]),
                ordinal.transform(df_needs_category[['UNIT_GROUP', 'CMPNT_MATL_TYPE_CATEGORY']])
            ])

            probs = model.predict_proba(X)
            preds = model.predict(X)
            scores = np.max(probs, axis=1)

            df.loc[category_idx, 'AI_FINAL_CATEGORY'] = np.where(scores < 0.6, 'Other', preds)
            df.loc[category_idx, 'AI_FINAL_CATEGORY_CONFIDENCE'] = scores
            df.loc[category_idx, 'AI_MATCHING_REASON_FINAL_CATEGORY'] = np.where(
                scores < 0.6, 'Low Confidence', 'lightgbm_Bert_RPM_Category_model'
            )

            del descs, embeddings, probs, preds, X
            gc.collect()

        # --- Predict Subcategory ---
        for cat in ['CHM', 'PKG', 'FNW']:
            subcat_idx = (df['AI_FINAL_CATEGORY'] == cat) & (df['needs_subcategory_model'] == True)
            subcat_df = df[subcat_idx].copy()

            if subcat_df.empty:
                continue

            sub_model = subcategory_models[cat]["model"]
            sub_encoder = subcategory_models[cat]["encoder"]
            sub_scaler = subcategory_models[cat]["scaler"]

            descs = subcat_df['CMPNT_MATL_DESC_CLEAN'].astype(str).tolist()
            embeddings = encode_descriptions(encoder, descs)

            X_sub = np.hstack([
                embeddings,
                sub_scaler.transform(subcat_df[['CMPNT_MATL_DESC_LEN']]),
                sub_encoder.transform(subcat_df[['UNIT_GROUP', 'CMPNT_MATL_TYPE_CATEGORY']])
            ])

            probs = sub_model.predict_proba(X_sub)
            preds = sub_model.predict(X_sub)
            scores = np.max(probs, axis=1)

            df.loc[subcat_idx, 'AI_FINAL_SUBCATEGORY'] = np.where(scores < 0.6, 'Other', preds)
            df.loc[subcat_idx, 'AI_FINAL_SUBCATEGORY_CONFIDENCE'] = scores
            df.loc[subcat_idx, 'AI_MATCHING_REASON_FINAL_SUBCATEGORY'] = np.where(
                scores < 0.6, 'Low Confidence', f"RPM_Category_model_full_{cat}_V1"
            )

            del descs, embeddings, probs, preds, X_sub
            gc.collect()

        # --- Fallback for other categories ---
        fallback_idx = (df['needs_subcategory_model'] == True) & (~df['AI_FINAL_CATEGORY'].isin(['CHM', 'PKG', 'FNW']))
        df.loc[fallback_idx, 'AI_FINAL_SUBCATEGORY'] = df.loc[fallback_idx, 'AI_FINAL_CATEGORY']
        df.loc[fallback_idx, 'AI_FINAL_SUBCATEGORY_CONFIDENCE'] = 0
        df.loc[fallback_idx, 'AI_MATCHING_REASON_FINAL_SUBCATEGORY'] = 'Fallback'

        output_path = os.path.join(final_output_dir, f"predicted_{f}")
        df.to_parquet(output_path, index=False)
        
        # Save CSV in working directory -> shows up in 'user_logs' in Azure ML
        #user_log_dir = "user_logs"
        #os.makedirs(user_log_dir, exist_ok=True)

        # Save CSV there
        #csv_output_path = os.path.join(user_log_dir, f"predicted_{f.replace('.parquet', '.csv')}")
        #df.to_csv(csv_output_path, index=False)
        # # Save summary CSV log
        # csv_log_path = os.path.join(log_dir, f"summary_{f.replace('.parquet', '.csv')}")
        # df.to_csv(csv_log_path, index=False)
        # # Step 5: Save logs
        # os.makedirs("logs/qc_nlp_list", exist_ok=True)
        # df.to_csv("log/qc_nlp_list/test.csv", index=False)

        # log(f"💾 Saved prediction: {output_path} (total rows: {len(df)})")
        # log(f"📝 Saved CSV summary: {csv_log_path}")

        del df
        gc.collect()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_path", required=True)
    parser.add_argument("--final_output_dir", required=True)
    args = parser.parse_args()

    main(args.input_path, args.final_output_dir)