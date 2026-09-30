import os
with open("app.py", "r", encoding="utf-8") as f:
    text = f.read()

extract_api_code = """
@app.post("/api/extract_symptoms")
def extract_symptoms():
    d = request.json or {}
    text = (d.get("text") or "").strip()
    if not text:
        return jsonify(symptoms=[])

    # First try LLM if Gemini key is available
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        try:
            import requests
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
            prompt = f"Extract the exact symptoms mentioned in the following text. You MUST return ONLY a JSON list of strings exactly matching the official symptom list. If none match, return [].\\nText: '{text}'\\nOfficial List: {SYMPTOMS}"
            resp = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=10)
            if resp.ok:
                resp_json = resp.json()
                content = resp_json["candidates"][0]["content"]["parts"][0]["text"]
                
                # Simple extraction of the JSON array from response
                import re
                match = re.search(r"\[.*?\]", content, re.DOTALL)
                if match:
                    llm_syms = json.loads(match.group(0))
                    valid_syms = [s for s in llm_syms if s in SYMPTOMS]
                    return jsonify(symptoms=valid_syms, method="gemini")
        except Exception as e:
            print("LLM Extraction failed, falling back to dict:", e)

    # Fallback to comprehensive dictionary matching
    synonymMap = {
        "Fever": ["fever", "bukhar", "taap", "garam", "hot", "?????", "???", "???"],
        "Cough": ["cough", "khansi", "khokla", "?????", "?????", "??"],
        "Nasal discharge": ["nasal", "naak", "sardi", "??? ????", "??? ????", "?????", "???"],
        "Difficulty breathing": ["breathing", "saans", "shwas", "dhaap", "????", "?????", "???"],
        "Reduced appetite": ["appetite", "bhook", "kha nahi", "chara", "???", "???", "????", "?? ????"],
        "Weakness / lethargy": ["weakness", "lethargy", "kamzor", "sust", "????", "?????", "??????", "?????", "????"],
        "Diarrhoea": ["diarrhoea", "diarrhea", "dast", "loose motion", "julab", "????", "?????", "?????"],
        "Vomiting": ["vomit", "ulti", "okari", "?????", "????", "?????"],
        "Dehydration": ["dehydration", "pani", "tahan", "????", "????", "???????????"],
        "Excessive salivation": ["saliva", "drool", "lar", "laar", "thook", "???", "???", "???"],
        "Mouth lesions / sores": ["mouth", "chhale", "tond", "muh", "????", "????", "????", "????"],
        "Lameness / difficulty walking": ["lame", "limp", "langda", "chalne", "??????", "????", "???"],
        "Swelling": ["swelling", "sujan", "suj", "????", "???", "?????"],
        "Skin lesions / rash": ["rash", "chakatte", "pural", "khaj", "??????", "????", "????", "???"],
        "Eye discharge / redness": ["eye", "aankh", "dole", "lal", "???", "????", "???"],
        "Abnormal milk production": ["milk", "doodh", "dudh", "???", "???"],
        "Abortion / reproductive problem": ["abortion", "garbhpat", "pillu", "???????", "??????", "??????"],
        "Weight loss": ["weight", "vazan", "wazan", "barik", "???", "?????", "?????"],
        "High body temperature": ["high temperature", "tez bukhar", "kadak taap", "??? ?????", "??? ???"],
        "Ticks / external parasites": ["tick", "killi", "gochid", "parjivi", "??????", "?????", "??????", "???", "?????"]
    }
    
    text_lower = text.lower()
    matched = []
    for sym in SYMPTOMS:
        kws = synonymMap.get(sym, [sym.lower()])
        for kw in kws:
            if kw in text_lower:
                matched.append(sym)
                break
    
    return jsonify(symptoms=matched, method="dictionary")
"""

idx = text.find('@app.post("/api/cases/report")')
if idx != -1:
    text = text[:idx] + extract_api_code + "\n" + text[idx:]
    with open("app.py", "w", encoding="utf-8") as f:
        f.write(text)
    print("Endpoint added")
else:
    print("Failed to find injection point")
