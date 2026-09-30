"""Generate an evidence-informed *synthetic* bovine clinical dataset.

The probabilities below encode broad qualitative associations described in
clinical_sources.md. They are simulation assumptions, not measured prevalence
or a substitute for veterinary case records.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from pathlib import Path

DISEASES = [
    "FMD", "Lumpy Skin Disease", "Hemorrhagic Septicemia", "Mastitis",
    "Black Quarter / Blackleg", "Anthrax", "Healthy",
]
FEATURES = [
    "Animal_Species", "Age_Months", "Sex", "Herd_Size",
    "Days_Since_Last_Vaccination", "Vaccinated_FMD", "Vaccinated_HS",
    "Vaccinated_LSD", "Vaccinated_BQ", "Fever_High_Body_Temperature",
    "Cough", "Nasal_Discharge", "Difficulty_Breathing", "Reduced_Appetite",
    "Weakness_Lethargy", "Diarrhea", "Dehydration", "Excessive_Salivation",
    "Mouth_Lesions_Sores", "Lameness_Difficulty_Walking", "Swelling",
    "Skin_Lesions_Rash", "Eye_Discharge_Redness",
    "Abortion_Reproductive_Problem", "Abnormal_Milk_Production", "Weight_Loss",
    "Reduced_Rumination", "Ticks_External_Parasites", "Temperature_C",
    "Humidity_Percent", "Rainfall_mm", "Season",
]
SYMPTOMS = FEATURES[9:27]

# Ordered as SYMPTOMS; approximate probabilities for mild, moderate, severe
# cases, chosen to permit overlap and missing characteristic signs.
PROFILES = {
    # Each triple is the probability for mild, moderate, and severe cases,
    # aligned with SYMPTOMS: fever, cough, nasal, breathing, appetite, weakness,
    # diarrhea, dehydration, salivation, mouth lesions, lameness, swelling, skin,
    # eye, abortion, abnormal milk, weight loss, rumination.
    "FMD": [(.58,.80,.93),(.02,.06,.15),(.08,.20,.38),(.01,.05,.17),(.38,.68,.88),(.20,.47,.73),(.01,.04,.13),(.04,.18,.42),(.40,.73,.90),(.42,.75,.93),(.30,.63,.86),(.01,.04,.14),(.01,.05,.16),(.03,.11,.27),(.01,.04,.13),(.10,.34,.61),(.12,.36,.58),(.18,.43,.69)],
    "Lumpy Skin Disease": [(.42,.70,.88),(.04,.12,.25),(.37,.62,.81),(.04,.15,.34),(.34,.62,.82),(.36,.65,.84),(.03,.10,.24),(.06,.20,.43),(.10,.31,.59),(.05,.17,.34),(.13,.34,.61),(.10,.35,.64),(.70,.91,.98),(.43,.70,.87),(.04,.14,.32),(.20,.48,.72),(.16,.43,.69),(.18,.46,.72)],
    "Hemorrhagic Septicemia": [(.68,.90,.98),(.08,.22,.42),(.45,.72,.88),(.52,.79,.94),(.40,.69,.88),(.45,.73,.90),(.04,.13,.32),(.13,.34,.61),(.44,.75,.91),(.05,.14,.30),(.04,.13,.30),(.56,.82,.95),(.03,.11,.26),(.34,.64,.84),(.02,.07,.18),(.08,.24,.49),(.30,.58,.80),(.27,.56,.78)],
    "Mastitis": [(.12,.40,.73),(.01,.04,.10),(.02,.07,.17),(.01,.04,.13),(.24,.55,.82),(.15,.42,.70),(.01,.04,.13),(.03,.12,.32),(.01,.04,.12),(.01,.04,.12),(.26,.57,.82),(.72,.91,.97),(.01,.03,.08),(.02,.08,.20),(.02,.08,.22),(.82,.95,.98),(.15,.39,.67),(.16,.43,.70)],
    "Black Quarter / Blackleg": [(.36,.67,.85),(.02,.05,.14),(.02,.08,.21),(.02,.09,.32),(.28,.58,.80),(.34,.65,.86),(.01,.04,.14),(.04,.18,.44),(.01,.04,.14),(.01,.04,.14),(.39,.70,.88),(.48,.77,.93),(.01,.04,.12),(.01,.05,.15),(.01,.04,.13),(.03,.14,.37),(.04,.18,.43),(.10,.31,.61)],
    "Anthrax": [(.38,.70,.91),(.02,.07,.18),(.05,.15,.36),(.34,.67,.91),(.30,.62,.86),(.37,.72,.93),(.05,.19,.45),(.23,.53,.82),(.02,.08,.24),(.01,.05,.16),(.06,.20,.48),(.20,.47,.75),(.01,.04,.12),(.02,.08,.23),(.05,.18,.45),(.03,.13,.39),(.10,.34,.71),(.10,.31,.72)],
    "Healthy": [(.005,.02,.05),(.015,.04,.08),(.015,.04,.08),(.003,.01,.03),(.025,.07,.14),(.02,.06,.13),(.005,.02,.05),(.003,.015,.04),(.003,.015,.04),(.003,.015,.04),(.015,.045,.10),(.015,.04,.09),(.01,.03,.07),(.01,.03,.07),(.003,.01,.03),(.005,.02,.05),(.02,.06,.13),(.025,.075,.15)],
}

def _clip_normal(rng, mean, sd, low, high, size=None):
    return np.clip(rng.normal(mean, sd, size), low, high)

def _season_weather(rng, n):
    season = rng.choice(["Summer", "Monsoon", "Winter"], n, p=[.34,.36,.30])
    temp = np.empty(n); humid = np.empty(n); rain = np.empty(n)
    for i, s in enumerate(season):
        if s == "Summer":
            temp[i] = _clip_normal(rng, 33, 4.2, 20, 46); humid[i] = _clip_normal(rng, 45, 17, 15, 88); rain[i] = rng.gamma(1.2, 2.5)
        elif s == "Monsoon":
            temp[i] = _clip_normal(rng, 29, 3.2, 19, 39); humid[i] = _clip_normal(rng, 79, 11, 42, 99); rain[i] = rng.gamma(2.2, 19)
        else:
            temp[i] = _clip_normal(rng, 21, 4.0, 8, 32); humid[i] = _clip_normal(rng, 58, 16, 20, 95); rain[i] = rng.gamma(1.2, 5)
    return season, temp, np.clip(humid,0,100), np.clip(rain,0,250)

def generate_dataset(n_per_class=1800, seed=20260926):
    rng = np.random.default_rng(seed)
    rows = []
    for disease in DISEASES:
        n = n_per_class
        # Severity distribution is disease-agnostic enough to avoid a hidden key.
        severity = rng.choice([0,1,2], n, p=[.32,.48,.20])
        species_p = {"Hemorrhagic Septicemia":.54,"Lumpy Skin Disease":.69}.get(disease,.73)
        species = rng.choice(["Cow","Buffalo"],n,p=[species_p,1-species_p])
        sex = rng.choice(["Female","Male"],n,p=[.78,.22])
        age = np.empty(n)
        for i in range(n):
            if disease == "Black Quarter / Blackleg":
                age[i] = np.clip(rng.gamma(3.2, 8.0)+4, 2, 120)
            elif disease == "Mastitis":
                age[i] = np.clip(rng.normal(62, 25), 24, 180)
            else:
                age[i] = np.clip(rng.lognormal(np.log(42), .72), 1, 240)
        herd = np.clip(rng.negative_binomial(3, .28, n)+2, 2, 250)
        season,temp,humidity,rain = _season_weather(rng,n)

        # A few genuine population-level associations: HS often occurs in humid
        # monsoon conditions; BQ skews toward young stock; clinical mastitis
        # necessarily targets lactating females. All leave substantial overlap.
        if disease == "Hemorrhagic Septicemia":
            wet = rng.random(n) < .43
            season[wet] = "Monsoon"; temp[wet] = _clip_normal(rng,30,3,20,40,wet.sum()); humidity[wet] = _clip_normal(rng,80,10,40,99,wet.sum()); rain[wet] = rng.gamma(2,18,wet.sum())
        lactating = (sex == "Female") & (age >= 24)
        if disease == "Mastitis":
            sex = np.where(rng.random(n)<.985,"Female",sex)
            age = np.where(age < 24, rng.uniform(24,180,n), age)

        # Vaccination is modeled as an imperfect risk modifier and as a feature
        # with overlapping distributions; vaccinated disease cases remain.
        vax = {}
        for name, protection, relevant in [
            ("Vaccinated_FMD",.48,"FMD"),("Vaccinated_HS",.54,"Hemorrhagic Septicemia"),
            ("Vaccinated_LSD",.50,"Lumpy Skin Disease"),("Vaccinated_BQ",.48,"Black Quarter / Blackleg")]:
            p = .43 if disease == "Healthy" else (.23 if disease == relevant else .36)
            vax[name] = (rng.random(n) < p).astype(int)
        any_vax = np.column_stack(list(vax.values())).any(axis=1)
        days = np.where(any_vax, np.clip(rng.lognormal(np.log(190),.82,n),1,1100), rng.integers(180,1101,n)).astype(int)
        ticks = (rng.random(n) < np.where(disease=="Healthy",.24,.29)).astype(int)

        row = {
            "Animal_Species":species,"Age_Months":np.rint(age).astype(int),"Sex":sex,
            "Herd_Size":herd.astype(int),"Days_Since_Last_Vaccination":days,**vax,
            "Ticks_External_Parasites":ticks,"Temperature_C":np.round(temp,1),
            "Humidity_Percent":np.round(humidity,1),"Rainfall_mm":np.round(rain,1),"Season":season,
        }
        profile = PROFILES[disease]
        for j, symptom in enumerate(SYMPTOMS):
            probs = np.array([profile[j][s] for s in severity])
            # Continuous variation in expression; target-linked factors alter
            # risk probabilistically rather than making any symptom deterministic.
            probs = np.clip(probs * rng.lognormal(0,.16,n), .002, .985)
            if symptom == "Abnormal_Milk_Production":
                probs *= np.where((sex=="Female") & (age>=24),1,.06)
            if symptom == "Abortion_Reproductive_Problem":
                probs *= np.where((sex=="Female") & (age>=18),1,.025)
            if symptom == "Fever_High_Body_Temperature" and disease == "Hemorrhagic Septicemia":
                probs = np.clip(probs + (season=="Monsoon")*.035, .002,.985)
            if symptom == "Skin_Lesions_Rash" and disease == "Lumpy Skin Disease":
                probs = np.clip(probs + (species=="Buffalo")*.01, .002,.985)
            row[symptom] = (rng.random(n) < probs).astype(int)

        # Rare coincidental observations preserve nonspecific signs in controls.
        frame = pd.DataFrame(row)
        frame["Disease"] = disease
        rows.append(frame[FEATURES+ ["Disease"]])
    result = pd.concat(rows,ignore_index=True)
    result = result.sample(frac=1,random_state=seed).reset_index(drop=True)
    return result

if __name__ == "__main__":
    import argparse
    from pathlib import Path
    parser=argparse.ArgumentParser(); parser.add_argument("--rows-per-class",type=int,default=1800); parser.add_argument("--seed",type=int,default=20260926); parser.add_argument("--output",default=str(Path(__file__).resolve().parent/"data"/"livestock_clinical_synthetic.csv"))
    args=parser.parse_args(); out=Path(args.output); out.parent.mkdir(parents=True,exist_ok=True)
    df=generate_dataset(args.rows_per_class,args.seed); df.to_csv(out,index=False)
    print(f"Wrote {len(df):,} synthetic rows to {out}")
