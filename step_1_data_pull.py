import os
import argparse
from azureml.core import Run, Dataset

def main(output_path):
    # Get run context and workspace
    run = Run.get_context()
    ws = run.experiment.workspace

    # Get dataset by name
    dataset = Dataset.get_by_name(ws, name='harmonized_bom_data_asset')

    # Ensure output directory exists and download dataset
    os.makedirs(output_path, exist_ok=True)
    dataset.download(target_path=output_path, overwrite=True)

    print(f" Dataset downloaded to: {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output_path", type=str, required=True)
    args = parser.parse_args()

    main(args.output_path)