"""Individual-animal disease classification inference API."""
from __future__ import annotations
from pathlib import Path
import joblib
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parent
MODEL_PATH=ROOT/"models"/"disease_classifier.joblib"
CLASSES=["FMD","Lumpy Skin Disease","Hemorrhagic Septicemia","Mastitis","Black Quarter / Blackleg","Anthrax","Healthy"]
FEATURES=["Animal_Species","Age_Months","Sex","Herd_Size","Days_Since_Last_Vaccination","Vaccinated_FMD","Vaccinated_HS","Vaccinated_LSD","Vaccinated_BQ","Fever_High_Body_Temperature","Cough","Nasal_Discharge","Difficulty_Breathing","Reduced_Appetite","Weakness_Lethargy","Diarrhea","Dehydration","Excessive_Salivation","Mouth_Lesions_Sores","Lameness_Difficulty_Walking","Swelling","Skin_Lesions_Rash","Eye_Discharge_Redness","Abortion_Reproductive_Problem","Abnormal_Milk_Production","Weight_Loss","Reduced_Rumination","Ticks_External_Parasites","Temperature_C","Humidity_Percent","Rainfall_mm","Season"]
CATEGORICAL={"Animal_Species":{"Cow","Buffalo"},"Sex":{"Male","Female"},"Season":{"Summer","Monsoon","Winter"}}
BINARY={"Vaccinated_FMD","Vaccinated_HS","Vaccinated_LSD","Vaccinated_BQ","Fever_High_Body_Temperature","Cough","Nasal_Discharge","Difficulty_Breathing","Reduced_Appetite","Weakness_Lethargy","Diarrhea","Dehydration","Excessive_Salivation","Mouth_Lesions_Sores","Lameness_Difficulty_Walking","Swelling","Skin_Lesions_Rash","Eye_Discharge_Redness","Abortion_Reproductive_Problem","Abnormal_Milk_Production","Weight_Loss","Reduced_Rumination","Ticks_External_Parasites"}

def _bundle(path=None):
    path=Path(path) if path else MODEL_PATH
    if not path.exists(): raise FileNotFoundError(f"Trained disease model not found: {path}. Run models/train_disease_model.py first.")
    return joblib.load(path)

def predict_disease(input_data:dict, model_path=None):
    """Return disease, calibrated confidence, and percentages for all classes.

    Extra mapping keys are ignored; all 32 declared features are required.
    This function does not return triage, escalation, or outbreak decisions.
    """
    missing=[f for f in FEATURES if f not in input_data]
    if missing: raise ValueError("Missing required input features: "+", ".join(missing))
    record={f:input_data[f] for f in FEATURES}
    for name,allowed in CATEGORICAL.items():
        if record[name] not in allowed: raise ValueError(f"{name} must be one of {sorted(allowed)}")
    for name in BINARY:
        if record[name] not in (0,1): raise ValueError(f"{name} must be 0 or 1")
    for name in ["Age_Months","Herd_Size","Days_Since_Last_Vaccination","Temperature_C","Humidity_Percent","Rainfall_mm"]:
        value=float(record[name])
        if not np.isfinite(value): raise ValueError(f"{name} must be finite")
        record[name]=value
    if record["Age_Months"]<0 or record["Herd_Size"]<1 or record["Days_Since_Last_Vaccination"]<0 or record["Rainfall_mm"]<0 or not 0<=record["Humidity_Percent"]<=100:
        raise ValueError("Numeric inputs are outside supported physical bounds")
    bundle=_bundle(model_path); clf=bundle["pipeline"]; temp=float(bundle["temperature"]); classes=bundle["classes"]
    raw=clf.predict_proba(pd.DataFrame([record],columns=FEATURES))[0]
    classifier_classes=list(clf.named_steps["classifier"].classes_)
    probs=raw[[classifier_classes.index(c) for c in classes]]
    logits=np.log(np.clip(probs,1e-12,1))/temp; logits-=logits.max(); probs=np.exp(logits); probs/=probs.sum()
    idx=int(np.argmax(probs)); rounded={name:round(float(p*100),1) for name,p in zip(classes,probs)}
    return {"predicted_disease":classes[idx],"confidence":rounded[classes[idx]],"probabilities":rounded}

predict_health_status=predict_disease  # small compatibility alias for existing import sites

if __name__=="__main__":
    # Provide schema-oriented usage instead of silently making up a sample case.
    print("Call predict_disease(mapping_with_all_32_features); see README.md for the input schema.")
