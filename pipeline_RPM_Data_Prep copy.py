from azureml.core import Workspace, Experiment, Environment, Dataset
from azureml.pipeline.core import Pipeline, PipelineData
from azureml.pipeline.steps import PythonScriptStep
from azureml.core.runconfig import RunConfiguration
from azureml.core.authentication import ServicePrincipalAuthentication
from azureml.data import OutputFileDatasetConfig
from azureml.core import Datastore
from datetime import datetime
import sys

# === Workspace Config ===
SUBSCRIPTION_ID = "a8d518a9-4587-4ba2-9a60-68b980c2f000"
RESOURCE_GROUP = "AZR-WDZ-DTO-AML-Development"
WORKSPACE_NAME = "AML-DTO-Marmot-dev"

# === Initialize Workspace ===
def get_workspace(use_sp_auth=False, args=None):
    if use_sp_auth and args and len(args) >= 4:
        tenant_id = args[1]
        client_id = args[2]
        client_secret = args[3]
        sp_auth = ServicePrincipalAuthentication(
            tenant_id=tenant_id,
            service_principal_id=client_id,
            service_principal_password=client_secret
        )
        return Workspace(
            subscription_id=SUBSCRIPTION_ID,
            resource_group=RESOURCE_GROUP,
            workspace_name=WORKSPACE_NAME,
            auth=sp_auth
        )
    else:
        return Workspace(
            subscription_id=SUBSCRIPTION_ID,
            resource_group=RESOURCE_GROUP,
            workspace_name=WORKSPACE_NAME
        )

ws = get_workspace(use_sp_auth=(len(sys.argv) > 3), args=sys.argv)
#compute_target = ws.compute_targets["llm-gpu-cluster-2"]
env = Environment.get(workspace=ws, name="Bom_X_Evaluator")
compute_target = ws.compute_targets["rpmdataprocess"]
default_ds = Datastore.get(ws, "xbomrefadlsg2")
run_config = RunConfiguration()
run_config.target = compute_target.name
run_config.environment = env

today = datetime.today().strftime("%d%m%Y")

# === Datastore and Uploads ===
datastore = ws.get_default_datastore()

# Upload mapping and abbreviation CSVs
mapping_local = './local_data/high_conf_direct_mapping.csv'
abbrev_local = './local_data/abbreviation_expension_updated.csv'

datastore.upload_files(
    files=[mapping_local],
    target_path='high_conf_map',
    overwrite=True,
    show_progress=True
)

datastore.upload_files(
    files=[abbrev_local],
    target_path='abbrev_map',
    overwrite=True,
    show_progress=True
)

# === Dataset References ===
high_conf_dataset = Dataset.File.from_files((datastore, "high_conf_map"))
abbr_dataset = Dataset.File.from_files((datastore, "abbrev_map"))

mapping_csv_mount = high_conf_dataset.as_named_input("high_conf_map").as_mount()
abbrev_csv_mount = abbr_dataset.as_named_input("abbrev_map").as_mount()

# === PipelineData for Intermediate Outputs ===
step1_out = PipelineData(name="step1_output", is_directory=True)
step1a_final_output = PipelineData(name="step1a_final_output", is_directory=True)
step1a_key_output_temp = PipelineData(name="step1a_key_output_temp", is_directory=True)
step2_final_output = PipelineData(name="step2_final_output", is_directory=True)
step3_final_output = PipelineData(name="step3_final_output", is_directory=True)

# === Step 4 Output (Final Merged Output) ===
final_merged_output = OutputFileDatasetConfig(
    name="final_merged_predictions",
    destination=(default_ds, f"hbom_category_prediction/hbom_category_prediction_{today}/")
)

# === Step 1: Pull Dataset ===
step1_pull = PythonScriptStep(
    name="Step 1 - Pull Dataset",
    script_name="step_1_data_pull.py",
    source_directory="scripts",
    arguments=["--output_path", step1_out],
    outputs=[step1_out],
    compute_target=compute_target,
    runconfig=run_config,
    allow_reuse=False
)

# === Step 1a: Merge & Map ===
step1a_merge = PythonScriptStep(
    name="Step 1a - Merge & Map",
    script_name="step_1a_extract_and_merge.py",
    source_directory="scripts",
    arguments=[
        "--input_path", step1_out,
        "--mapping_csv", mapping_csv_mount,
        "--key_output", step1a_key_output_temp,
        "--final_output", step1a_final_output
    ],
    inputs=[step1_out, mapping_csv_mount],
    outputs=[step1a_final_output, step1a_key_output_temp],
    compute_target=compute_target,
    runconfig=run_config,
    allow_reuse=False
)

# === Step 2: Preprocessing ===
step2_preprocess = PythonScriptStep(
    name="Step 2 - Preprocessing",
    script_name="step2_pre_process.py",
    source_directory="scripts",
    arguments=[
        "--input_path", step1a_final_output,
        "--abbrev_map", abbrev_csv_mount,
        "--output_path", step2_final_output
    ],
    inputs=[step1a_final_output, abbrev_csv_mount],
    outputs=[step2_final_output],
    compute_target=compute_target,
    runconfig=run_config,
    allow_reuse=False
)

# === Step 3: Inference Prediction ===
step3_inference = PythonScriptStep(
    name="Step 3 - Inference Prediction",
    script_name="step3_inference_run.py",
    source_directory="scripts",
    arguments=[
        "--input_path", step2_final_output,
        "--final_output_dir", step3_final_output
    ],
    inputs=[step2_final_output],
    outputs=[step3_final_output],
    compute_target=compute_target,
    runconfig=run_config,
    allow_reuse=False
)

# === Step 4: Merge Inference Results with Key Files (Final Output) ===
step4_merge = PythonScriptStep(
    name="Step 4 - Merge Inference Results with Key Files",
    script_name="step_4_merge_with_key_files.py",
    source_directory="scripts",
    arguments=[
        "--inference_output_dir", step3_final_output,
        "--key_output_dir", step1a_key_output_temp,
        "--final_output_dir", final_merged_output
    ],
    inputs=[step3_final_output, step1a_key_output_temp],
    outputs=[final_merged_output],
    compute_target=compute_target,
    runconfig=run_config,
    allow_reuse=False
)

# === Build and Run Pipeline ===
pipeline = Pipeline(workspace=ws, steps=[
    step1_pull,
    step1a_merge,
    step2_preprocess,
    step3_inference,
    step4_merge
])
pipeline.validate()

experiment = Experiment(ws, name="bom_pipeline_pull_merge_preprocess")
run = experiment.submit(pipeline)
run.wait_for_completion(show_output=True)