"""Train, calibrate, and evaluate the individual disease classifier.

Model selection uses only the training partition and 5-fold CV. Validation is
used for scalar temperature calibration. The test partition is evaluated once
at the end and is never used for model or calibration selection.
"""
from __future__ import annotations
import json, sys, warnings
import importlib.util
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar
from sklearn.base import clone
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier, HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, balanced_accuracy_score, classification_report,
    confusion_matrix, f1_score, log_loss, precision_score, recall_score, brier_score_loss)
from sklearn.model_selection import StratifiedKFold, cross_validate, train_test_split, GridSearchCV
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from generate_dataset import DISEASES, FEATURES, SYMPTOMS, generate_dataset
SEED=20260926
CATEGORICAL=["Animal_Species","Sex","Season"]
NUMERIC=[c for c in FEATURES if c not in CATEGORICAL]

def pipeline(model):
    prep=ColumnTransformer([
        ("categorical",Pipeline([("imputer",SimpleImputer(strategy="most_frequent")),("onehot",OneHotEncoder(handle_unknown="ignore"))]),CATEGORICAL),
        ("numeric",Pipeline([("imputer",SimpleImputer(strategy="median")),("scale",StandardScaler())]),NUMERIC),
    ],remainder="drop",sparse_threshold=0)
    return Pipeline([("preprocess",prep),("classifier",model)])

def temp_probs(p,t):
    z=np.log(np.clip(p,1e-12,1))/t; z-=z.max(axis=1,keepdims=True); e=np.exp(z); return e/e.sum(axis=1,keepdims=True)

def fit_temperature(p,y):
    yidx=pd.Categorical(y,categories=DISEASES).codes
    opt=minimize_scalar(lambda t: multiclass_log_loss(yidx,temp_probs(p,t)),bounds=(.35,4.0),method="bounded")
    return float(opt.x)

def multiclass_log_loss(y_index,p):
    return float(-np.log(np.clip(p[np.arange(len(y_index)),y_index],1e-15,1)).mean())

def reliability_table(y,p,bins=10):
    yi=pd.Categorical(y,categories=DISEASES).codes; pred=p.argmax(axis=1); conf=p.max(axis=1); correct=(pred==yi); out=[]
    edges=np.linspace(0,1,bins+1)
    for i in range(bins):
        mask=(conf>=edges[i])&(conf<(edges[i+1] if i<bins-1 else edges[i+1]+1e-10))
        if mask.any(): out.append({"bin_lower":float(edges[i]),"bin_upper":float(edges[i+1]),"count":int(mask.sum()),"mean_confidence":float(conf[mask].mean()),"accuracy":float(correct[mask].mean())})
    return out

def sampled_near_duplicate_scan(df,n_pairs=5000):
    rng=np.random.default_rng(SEED+1); a=rng.integers(0,len(df),n_pairs); b=rng.integers(0,len(df),n_pairs)
    cats=["Animal_Species","Sex","Season"]
    binary=[c for c in FEATURES if c in [*CATEGORICAL,"Vaccinated_FMD","Vaccinated_HS","Vaccinated_LSD","Vaccinated_BQ",*SYMPTOMS,"Ticks_External_Parasites"]]
    numeric=["Age_Months","Herd_Size","Days_Since_Last_Vaccination","Temperature_C","Humidity_Percent","Rainfall_mm"]
    same=(a!=b)&(df.Disease.to_numpy()[a]==df.Disease.to_numpy()[b])
    for c in cats: same &= df[c].to_numpy()[a]==df[c].to_numpy()[b]
    mismatches=np.zeros(n_pairs,dtype=int)
    for c in binary: mismatches+=(df[c].to_numpy()[a]!=df[c].to_numpy()[b])
    same &= mismatches<=1
    limits={"Age_Months":2,"Herd_Size":0,"Days_Since_Last_Vaccination":7,"Temperature_C":.5,"Humidity_Percent":2,"Rainfall_mm":2}
    for c in numeric: same &= np.abs(df[c].to_numpy()[a]-df[c].to_numpy()[b])<=limits[c]
    return {"method":"5,000 random row-pairs; same target and categories, at most one binary mismatch, and tight numeric tolerances","pairs_screened":n_pairs,"near_duplicate_pairs_found":int(same.sum())}

def target_associations(df):
    encoded=df[FEATURES].copy()
    for c in CATEGORICAL:
        encoded[c]=encoded[c].astype(str)
    encoded=pd.get_dummies(encoded,columns=CATEGORICAL,dtype=float)
    target=pd.get_dummies(pd.Categorical(df.Disease,categories=DISEASES),dtype=float)
    pairs=[]
    for feature in encoded.columns:
        raw=next((c for c in CATEGORICAL if feature.startswith(c+"_")),feature)
        x=encoded[feature].astype(float)
        for disease in DISEASES:
            corr=float(x.corr(target[disease]))
            if np.isfinite(corr): pairs.append({"feature":raw,"encoded_level":feature if raw in CATEGORICAL else None,"disease":disease,"correlation":corr,"absolute_correlation":abs(corr)})
    pairs.sort(key=lambda item:item["absolute_correlation"],reverse=True)
    return pairs

def ece_top_label(p,y,bins=10):
    y=np.asarray(y); pred=p.argmax(axis=1); conf=p.max(axis=1); correct=(pred==pd.Categorical(y,categories=DISEASES).codes)
    result=[]
    for lo in np.linspace(0,1,bins+1)[:-1]:
        hi=lo+1/bins; ix=(conf>=lo)&(conf<(hi if hi<1 else hi+1e-10))
        if ix.any(): result.append((float(ix.mean()),float(abs(conf[ix].mean()-correct[ix].mean()))))
    return float(sum(w*e for w,e in result))

def ordered_proba(model,X):
    raw=model.predict_proba(X); model_classes=list(model.named_steps["classifier"].classes_)
    return raw[:,[model_classes.index(c) for c in DISEASES]]

def metrics(y,p):
    pred=np.array(DISEASES)[p.argmax(axis=1)]
    return {"accuracy":float(accuracy_score(y,pred)),"balanced_accuracy":float(balanced_accuracy_score(y,pred)),
        "macro_precision":float(precision_score(y,pred,labels=DISEASES,average="macro",zero_division=0)),
        "macro_recall":float(recall_score(y,pred,labels=DISEASES,average="macro",zero_division=0)),
        "macro_f1":float(f1_score(y,pred,labels=DISEASES,average="macro")),"weighted_f1":float(f1_score(y,pred,labels=DISEASES,average="weighted")),
        "log_loss":multiclass_log_loss(pd.Categorical(y,categories=DISEASES).codes,p),"multiclass_brier":float(np.mean(np.sum((p-np.eye(len(DISEASES))[pd.Categorical(y,categories=DISEASES).codes])**2,axis=1))),"top_label_ece_10_bins":ece_top_label(p,y)}

def main():
    data_path=ROOT/"data"/"livestock_clinical_synthetic.csv"
    if not data_path.exists():
        df=generate_dataset(); data_path.parent.mkdir(exist_ok=True); df.to_csv(data_path,index=False)
    df=pd.read_csv(data_path)
    if list(df.columns)!=FEATURES+["Disease"]: raise ValueError("Dataset columns differ from the exact 32-feature schema")
    if set(df.Disease)!=set(DISEASES): raise ValueError("Dataset target labels must match the exact seven classes")
    if df[FEATURES].isna().any().any(): raise ValueError("Training dataset contains missing input values")
    # Stratified, independent partitions: 70/15/15.
    X=df[FEATURES]; y=df["Disease"]
    X_train,X_rem,y_train,y_rem=train_test_split(X,y,test_size=.30,stratify=y,random_state=SEED)
    X_val,X_test,y_val,y_test=train_test_split(X_rem,y_rem,test_size=.50,stratify=y_rem,random_state=SEED+1)
    cv=StratifiedKFold(n_splits=5,shuffle=True,random_state=SEED)
    models={
        "Regularized Logistic Regression":LogisticRegression(C=1.0,max_iter=1600,class_weight="balanced",solver="lbfgs"),
        "Random Forest":RandomForestClassifier(n_estimators=240,max_depth=14,min_samples_leaf=5,max_features=.8,class_weight="balanced_subsample",n_jobs=-1,random_state=SEED),
        "Extra Trees":ExtraTreesClassifier(n_estimators=240,max_depth=14,min_samples_leaf=5,max_features=.8,class_weight="balanced",n_jobs=-1,random_state=SEED),
        "HistGradientBoosting":HistGradientBoostingClassifier(max_iter=160,max_leaf_nodes=15,l2_regularization=2.,learning_rate=.08,early_stopping=True,random_state=SEED),
    }
    scores={}; candidates={}
    for name,est in models.items():
        pipe=pipeline(est); candidates[name]=pipe
        cvout=cross_validate(pipe,X_train,y_train,cv=cv,scoring={"accuracy":"accuracy","macro_f1":"f1_macro","log_loss":"neg_log_loss"},n_jobs=1,return_train_score=False)
        scores[name]={"accuracy_mean":float(cvout["test_accuracy"].mean()),"accuracy_std":float(cvout["test_accuracy"].std()),
                      "macro_f1_mean":float(cvout["test_macro_f1"].mean()),"macro_f1_std":float(cvout["test_macro_f1"].std()),
                      "log_loss_mean":float(-cvout["test_log_loss"].mean()),"log_loss_std":float(cvout["test_log_loss"].std())}
    # Tune a compact candidate set on CV, optimizing log loss only.
    order=sorted(scores,key=lambda k:(scores[k]["log_loss_mean"],scores[k]["log_loss_std"]))
    top=order[:2]; tuned=[]
    params={
        "Regularized Logistic Regression":{"classifier__C":[.25,1.,4.]},
        "Random Forest":{"classifier__max_depth":[10,14,18],"classifier__min_samples_leaf":[4,8]},
        "Extra Trees":{"classifier__max_depth":[10,14,18],"classifier__min_samples_leaf":[4,8]},
        "HistGradientBoosting":{"classifier__max_leaf_nodes":[11,15,21],"classifier__l2_regularization":[1.,3.]},
    }
    tuned_summaries={}
    for name in top:
        search=GridSearchCV(candidates[name],params[name],scoring="neg_log_loss",cv=cv,n_jobs=1,refit=True,return_train_score=False)
        search.fit(X_train,y_train); tuned.append((name,search.best_estimator_,-float(search.best_score_),search.best_params_))
        tuned_summaries[name]={"best_cv_log_loss":-float(search.best_score_),"best_params":search.best_params_}
    selected=sorted(tuned,key=lambda z:z[2])[0]; best_name,best_model,best_cv,best_params=selected
    # Validation-set-only temperature scaling; no refitting on validation.
    val_raw=ordered_proba(best_model,X_val); temp=fit_temperature(val_raw,y_val); val_cal=temp_probs(val_raw,temp)
    # The untouched final test partition is evaluated only here.
    test_p=temp_probs(ordered_proba(best_model,X_test),temp)
    train_p=temp_probs(ordered_proba(best_model,X_train),temp)
    # Separate generated robustness sample: condition-slice results are stress
    # checks for this simulator, not independent clinical validation.
    stress=generate_dataset(n_per_class=120,seed=SEED+77)
    stress_p=temp_probs(ordered_proba(best_model,stress[FEATURES]),temp)
    stress_pred=np.array(DISEASES)[stress_p.argmax(axis=1)]
    robustness={"notice":"Fresh synthetic scenarios from the same stated generator; simulator robustness only.",
      "overall":metrics(stress.Disease,stress_p),"by_species":{},"by_sex":{},"by_season":{},"by_vaccination_status":{}}
    for key in ["Animal_Species","Sex","Season"]:
        for value in sorted(stress[key].unique()):
            ix=(stress[key].to_numpy()==value)
            if ix.sum()>=20: robustness[{"Animal_Species":"by_species","Sex":"by_sex","Season":"by_season"}[key]][str(value)]={"n":int(ix.sum()),"accuracy":float(accuracy_score(stress.Disease.to_numpy()[ix],stress_pred[ix])),"macro_f1":float(f1_score(stress.Disease.to_numpy()[ix],stress_pred[ix],labels=DISEASES,average="macro",zero_division=0))}
    vaccinated=stress[["Vaccinated_FMD","Vaccinated_HS","Vaccinated_LSD","Vaccinated_BQ"]].any(axis=1).to_numpy()
    for label,ix in [("any_recorded_vaccination",vaccinated),("no_recorded_vaccination",~vaccinated)]:
        robustness["by_vaccination_status"][label]={"n":int(ix.sum()),"accuracy":float(accuracy_score(stress.Disease.to_numpy()[ix],stress_pred[ix])),"macro_f1":float(f1_score(stress.Disease.to_numpy()[ix],stress_pred[ix],labels=DISEASES,average="macro",zero_division=0))}
    train_m=metrics(y_train,train_p); val_m=metrics(y_val,val_cal); test_m=metrics(y_test,test_p)
    outdir=ROOT/"models"; outdir.mkdir(exist_ok=True)
    bundle={"pipeline":best_model,"temperature":temp,"classes":DISEASES,"features":FEATURES,"categorical_features":CATEGORICAL,"schema_version":"1.0"}
    joblib.dump(bundle,outdir/"disease_classifier.joblib",compress=3)

    # Dataset quality summary and descriptive checks.
    counts=df.Disease.value_counts().reindex(DISEASES).to_dict()
    quality={"synthetic_data_notice":"All rows are generated examples, not real animal records.","rows":len(df),"feature_count":len(FEATURES),"target_classes":DISEASES,"class_counts":counts,
      "exact_schema_match":list(df.columns)==FEATURES+["Disease"],"unexpected_or_forbidden_columns":[],
      "missing_cells":int(df.isna().sum().sum()),"exact_duplicate_rows":int(df.duplicated().sum()),"duplicate_input_rows":int(df.duplicated(FEATURES).sum()),
      "invalid_binary_cells":int(sum((~df[c].isin([0,1])).sum() for c in [*SYMPTOMS,"Ticks_External_Parasites","Vaccinated_FMD","Vaccinated_HS","Vaccinated_LSD","Vaccinated_BQ"])),
      "near_duplicate_screen":sampled_near_duplicate_scan(df),
      "mastitis_female_fraction":float((df.loc[df.Disease=="Mastitis","Sex"]=="Female").mean()),
      "symptom_prevalence_by_disease":df.groupby("Disease")[SYMPTOMS].mean().reindex(DISEASES).round(4).to_dict(orient="index"),
      "vaccination_prevalence_by_disease":df.groupby("Disease")[["Vaccinated_FMD","Vaccinated_HS","Vaccinated_LSD","Vaccinated_BQ"]].mean().reindex(DISEASES).round(4).to_dict(orient="index"),
      "species_by_disease":pd.crosstab(df.Disease,df.Animal_Species,normalize="index").reindex(DISEASES).round(4).to_dict(orient="index"),
      "sex_by_disease":pd.crosstab(df.Disease,df.Sex,normalize="index").reindex(DISEASES).round(4).to_dict(orient="index"),
      "season_distribution":df.Season.value_counts(normalize=True).round(4).to_dict(),
      "numeric_describe":df[NUMERIC].describe().round(3).to_dict(),
      "largest_feature_target_correlations":target_associations(df)[:15],
      "high_correlation_flags_abs_over_0_6":[x for x in target_associations(df) if x["absolute_correlation"]>=.6],
      "split_sizes":{"train":len(X_train),"validation":len(X_val),"test":len(X_test)},
      "split_exact_input_overlap":int(len(set(map(tuple,X_train.values))&set(map(tuple,X_test.values))))}
    (ROOT/"reports").mkdir(exist_ok=True)
    (ROOT/"reports"/"dataset_quality.json").write_text(json.dumps(quality,indent=2),encoding="utf-8")
    test_pred=np.array(DISEASES)[test_p.argmax(axis=1)]
    cm=confusion_matrix(y_test,test_pred,labels=DISEASES).tolist()
    # Export coefficients from a selected linear model, if applicable.
    feature_strength=[]
    if hasattr(best_model.named_steps["classifier"],"coef_"):
        transformed=best_model.named_steps["preprocess"].get_feature_names_out()
        coefficient_rows=[]; grouped={f:[] for f in FEATURES}
        for ci,disease_name in enumerate(best_model.named_steps["classifier"].classes_):
            for term,coef in zip(transformed,best_model.named_steps["classifier"].coef_[ci]):
                feature=term.split("__",1)[-1]
                base=next((c for c in CATEGORICAL if feature.startswith(c+"_")),feature)
                if base not in FEATURES: continue
                coefficient_rows.append({"class":disease_name,"transformed_term":term,"raw_feature":base,"coefficient":float(coef)})
                grouped[base].append(abs(float(coef)))
        pd.DataFrame(coefficient_rows).to_csv(ROOT/"reports"/"linear_coefficients.csv",index=False)
        feature_strength=sorted([{"feature":k,"mean_absolute_coefficient":float(np.mean(v))} for k,v in grouped.items() if v],key=lambda x:x["mean_absolute_coefficient"],reverse=True)
    optional_models={name:("available" if importlib.util.find_spec(name) else "not installed; not benchmarked") for name in ["xgboost","lightgbm","catboost"]}
    eval_report={"important_limit":"Metrics measure performance on a held-out split of synthetic data generated by the same simulator. They are not evidence of clinical accuracy on real animals.","optional_gradient_boosting_libraries":optional_models,"robustness_stress_scenarios":robustness,
      "dataset":{"rows":len(df),"features":len(FEATURES),"class_counts":counts,"split_sizes":quality["split_sizes"]},
      "models_cv":scores,"tuning_top_models":tuned_summaries,"selected_model":best_name,"selected_hyperparameters":best_params,
      "selected_cv_log_loss":best_cv,"calibration":{"method":"one-parameter temperature scaling fitted on validation set","temperature":temp,"test_top_label_reliability_bins":reliability_table(y_test,test_p)},
      "train":train_m,"validation_calibrated":val_m,"test_final":test_m,
      "linear_feature_strength":feature_strength,
      "generalization_gaps":{"train_minus_validation_accuracy":train_m["accuracy"]-val_m["accuracy"],"train_minus_test_accuracy":train_m["accuracy"]-test_m["accuracy"],"train_minus_test_macro_f1":train_m["macro_f1"]-test_m["macro_f1"]},
      "per_class_test":classification_report(y_test,test_pred,labels=DISEASES,output_dict=True,zero_division=0),"confusion_matrix_labels":DISEASES,"confusion_matrix":cm}
    (ROOT/"reports"/"evaluation_report.json").write_text(json.dumps(eval_report,indent=2),encoding="utf-8")
    # Human-readable report summary.
    lines=["# Evaluation report","","> All scores below are internal synthetic-split results. They do not estimate performance on real clinical cases.","",f"- Rows: {len(df):,}; 32 input features; 7 target classes.",f"- Split: train {len(X_train):,} / validation {len(X_val):,} / test {len(X_test):,}.",f"- Selected model: {best_name}; validation-fitted temperature = {temp:.3f}.",f"- Optional packages: {json.dumps(optional_models)}.","","## Cross-validation on training partition","","| Model | Accuracy mean ± SD | Macro F1 mean ± SD | Log loss mean ± SD |","|---|---:|---:|---:|"]
    for name,v in scores.items(): lines.append(f"| {name} | {v['accuracy_mean']:.3f} ± {v['accuracy_std']:.3f} | {v['macro_f1_mean']:.3f} ± {v['macro_f1_std']:.3f} | {v['log_loss_mean']:.3f} ± {v['log_loss_std']:.3f} |")
    lines += ["","## Final partitions (calibrated probabilities)","","| Metric | Train | Validation | Test |","|---|---:|---:|---:|"]
    for key in ["accuracy","balanced_accuracy","macro_precision","macro_recall","macro_f1","weighted_f1","log_loss","multiclass_brier","top_label_ece_10_bins"]: lines.append(f"| {key} | {train_m[key]:.4f} | {val_m[key]:.4f} | {test_m[key]:.4f} |")
    lines += ["","## Robustness scenarios","", "Fresh synthetic stress sample (same simulator; not clinical validation): "+json.dumps(robustness["overall"]),"", "## Calibration","", "Test top-label reliability bins (confidence compared with empirical accuracy):", "", "| Bin | N | Mean confidence | Accuracy |", "|---|---:|---:|---:|"]
    for item in eval_report["calibration"]["test_top_label_reliability_bins"]: lines.append(f"| {item['bin_lower']:.1f}–{item['bin_upper']:.1f} | {item['count']} | {item['mean_confidence']:.3f} | {item['accuracy']:.3f} |")
    lines += ["", "Linear coefficients are exported in `linear_coefficients.csv`; grouped absolute magnitudes are associational diagnostics, not causal explanations.","", "## Per-class test metrics","", "| Disease | Precision | Recall | F1 | Support |","|---|---:|---:|---:|---:|"]
    for d in DISEASES:
        v=eval_report["per_class_test"][d]; lines.append(f"| {d} | {v['precision']:.3f} | {v['recall']:.3f} | {v['f1-score']:.3f} | {int(v['support'])} |")
    lines += ["","## Confusion matrix","","Rows are actual classes; columns are predicted classes in the listed order:","", "`"+json.dumps(cm)+"`", "", "The dataset generator encodes broad clinical signs and overlaps based on the sources documented in `clinical_sources.md`. The synthetic probability assumptions are not epidemiological estimates. Clinical deployment requires evaluation on independently collected, veterinarian-labelled cases."]
    (ROOT/"reports"/"evaluation_report.md").write_text("\n".join(lines),encoding="utf-8")
    print(f"Selected {best_name}; test accuracy={test_m['accuracy']:.3f}, log_loss={test_m['log_loss']:.3f}; temperature={temp:.3f}")
    print(f"Wrote model and reports under {ROOT}")

if __name__=="__main__": main()
