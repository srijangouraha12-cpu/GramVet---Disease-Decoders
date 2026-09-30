# GramVet individual livestock disease classifier

This package predicts one of exactly seven individual-animal classes and returns a calibrated probability distribution over those classes:

`FMD`, `Lumpy Skin Disease`, `Hemorrhagic Septicemia`, `Mastitis`, `Black Quarter / Blackleg`, `Anthrax`, `Healthy`.

## Important data and clinical limitation

`data/livestock_clinical_synthetic.csv` is generated synthetic data, not real animal records. It was designed using qualitative clinical relationships from the sources in [clinical_sources.md](clinical_sources.md). The simulator's numerical rates are assumptions. Reported test and cross-validation results only describe held-out samples from that same simulator; they do not demonstrate real-world veterinary accuracy or calibration. Do not use the model as a diagnosis or deploy it in clinical workflows without independent veterinarian-labelled data and external validation.

## Inputs

The model consumes exactly the 32 raw fields below and no location, case, treatment, triage, escalation, outbreak, or identifier fields. Target labels are exactly the seven labels above.

```text
Animal_Species, Age_Months, Sex, Herd_Size, Days_Since_Last_Vaccination,
Vaccinated_FMD, Vaccinated_HS, Vaccinated_LSD, Vaccinated_BQ,
Fever_High_Body_Temperature, Cough, Nasal_Discharge, Difficulty_Breathing,
Reduced_Appetite, Weakness_Lethargy, Diarrhea, Dehydration, Excessive_Salivation,
Mouth_Lesions_Sores, Lameness_Difficulty_Walking, Swelling, Skin_Lesions_Rash,
Eye_Discharge_Redness, Abortion_Reproductive_Problem, Abnormal_Milk_Production,
Weight_Loss, Reduced_Rumination, Ticks_External_Parasites, Temperature_C,
Humidity_Percent, Rainfall_mm, Season
```

`Animal_Species` is `Cow` or `Buffalo`; `Sex` is `Male` or `Female`; `Season` is `Summer`, `Monsoon`, or `Winter`. Symptom, vaccination, and tick fields are binary `0/1` values.

## Reproduce the dataset and model

Python 3.10+ is recommended.

### Run the notebook

1. Download and extract `GramVet_ML_Package_Rebuilt.zip`.
2. Open `GramVet_ML/Run_GramVet_ML.ipynb` in JupyterLab, Jupyter Notebook, or VS Code with the Jupyter extension.
3. Select a Python 3.10+ kernel.
4. Run the notebook cells from top to bottom. The first setup cell installs dependencies; later cells generate the dataset, train/evaluate the model, show reports, and run a sample prediction.
5. Training can take several minutes because it benchmarks models with five-fold cross-validation.

The notebook locates the package folder by checking the current folder and its parents. Open it from the extracted package so that `generate_dataset.py`, `models/`, and `requirements.txt` are available.

### Run in Google Colab

1. Download both `GramVet_ML_Package_Rebuilt.zip` and `Run_GramVet_ML_Colab.ipynb`.
2. Open [Google Colab](https://colab.research.google.com/) and choose **File → Upload notebook**. Select `Run_GramVet_ML_Colab.ipynb`.
3. Run the first upload cell and choose `GramVet_ML_Package_Rebuilt.zip` from your computer.
4. Run the remaining cells in order. They install requirements, generate the data, train/evaluate the model, and show a sample prediction.
5. Run the final download cell to save a ZIP of the trained package to your computer. Colab's local files are temporary.

### Run from a terminal

```powershell
python -m pip install -r requirements.txt
python generate_dataset.py --rows-per-class 1800 --seed 20260926
python models/train_disease_model.py
```

The default dataset has 12,600 rows (1,800 per class). This is a balanced synthetic development sample, not an estimate of disease prevalence. Training creates:

- `models/disease_classifier.joblib`: fitted preprocessing pipeline, classifier, class order and validation-fitted temperature calibration scalar.
- `reports/dataset_quality.json`: input validation, distributions, missingness and duplicate summaries.
- `reports/evaluation_report.md` and `.json`: 5-fold CV comparison, tuned model, train/validation/test metrics, calibration, per-class metrics, confusion matrix and a separately generated simulator stress check.

The deterministic stratified split is 70% train, 15% validation, 15% test. Four models are benchmarked by 5-fold CV log loss. The two strongest initial candidates receive compact CV hyperparameter searches. The test partition is not used for model or calibration selection. A single temperature parameter is fitted to the validation partition; this is a calibration technique on synthetic data, not evidence that confidence is clinically calibrated.

## Inference

Train the package first, then call:

```python
from predict_disease import predict_disease

result = predict_disease({
    "Animal_Species": "Cow", "Age_Months": 48, "Sex": "Female", "Herd_Size": 12,
    "Days_Since_Last_Vaccination": 210,
    "Vaccinated_FMD": 1, "Vaccinated_HS": 1, "Vaccinated_LSD": 0, "Vaccinated_BQ": 1,
    "Fever_High_Body_Temperature": 1, "Cough": 0, "Nasal_Discharge": 0,
    "Difficulty_Breathing": 0, "Reduced_Appetite": 1, "Weakness_Lethargy": 1,
    "Diarrhea": 0, "Dehydration": 0, "Excessive_Salivation": 1,
    "Mouth_Lesions_Sores": 1, "Lameness_Difficulty_Walking": 1, "Swelling": 0,
    "Skin_Lesions_Rash": 0, "Eye_Discharge_Redness": 0,
    "Abortion_Reproductive_Problem": 0, "Abnormal_Milk_Production": 0,
    "Weight_Loss": 0, "Reduced_Rumination": 1, "Ticks_External_Parasites": 0,
    "Temperature_C": 29.0, "Humidity_Percent": 72.0, "Rainfall_mm": 18.0,
    "Season": "Monsoon",
})
```

Return schema:

```json
{
  "predicted_disease": "FMD",
  "confidence": 64.3,
  "probabilities": {
    "FMD": 64.3,
    "Lumpy Skin Disease": 8.1,
    "Hemorrhagic Septicemia": 10.2,
    "Mastitis": 4.0,
    "Black Quarter / Blackleg": 6.1,
    "Anthrax": 4.4,
    "Healthy": 2.9
  }
}
```

The values above illustrate the response shape only, not a fixed prediction. Probabilities are percentages rounded to one decimal place; the unrounded distribution sums to 100%. `confidence` equals the rounded probability of `predicted_disease`. All 32 fields are required; extra keys are ignored. Unknown categories, invalid binary values, and nonphysical numeric bounds raise `ValueError`.

## Scope

`predict_disease()` does not produce outbreak scores, triage, escalation, treatment, or vet-notification decisions. The historical `models/train_triage_model.py` and `models/train_escalation_model.py` files are retained untouched from the supplied archive, are not called by inference, and are not run by this pipeline. The old surveillance CSV has been removed from the deliverable so the package does not expose two competing dataset schemas.
