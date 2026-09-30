with open('templates/index.html', 'r', encoding='utf-8') as f:
    text = f.read()

start_idx = text.find('function matchSymptomsFromTranscript')
end_idx = text.find('</script>', start_idx)

if start_idx != -1 and end_idx != -1:
    old_func = text[start_idx:end_idx]
    
    new_func = """function matchSymptomsFromTranscript(text) {
    text = text.toLowerCase();
    
    const synonymMap = {
        "Fever": ["fever", "bukhar", "taap", "garam", "hot", "?????", "???", "???", "????"],
        "Loss of Appetite": ["appetite", "bhook", "not eating", "kha nahi", "khana", "jevan", "chara", "???", "???", "???? ????", "?? ????", "??? ????"],
        "Lethargy": ["lethargy", "weak", "kamzor", "thaka", "ashakt", "sust", "?????", "?????", "????", "?????", "????", "??"],
        "Reduced Milk Yield": ["milk", "doodh", "dudh", "kam doodh", "yield", "???", "?? ???", "??? ???", "??? ????"],
        "Lesions/Blisters on Mouth": ["blister", "mouth", "muh", "chhale", "lesion", "ulcer", "tond", "????", "????", "????", "????", "????"],
        "Lesions/Blisters on Feet": ["feet", "foot", "pair", "khur", "chhale", "blister", "paya", "???", "???", "???"],
        "Lameness": ["lame", "limp", "langda", "langdana", "??????", "????", "???"],
        "Excessive Salivation": ["saliva", "drool", "lar", "laar", "thook", "gala", "???", "???", "???"],
        "Skin Nodules/Lumps": ["nodule", "lump", "bump", "gath", "gaath", "sujan skin", "????", "????", "???", "?????"],
        "Swollen Lymph Nodes": ["lymph", "node", "gland", "sujan gland", "??????", "?????"],
        "High Fever": ["high fever", "tez bukhar", "kadak taap", "??? ?????", "??? ???"],
        "Sudden Death": ["death", "dead", "mar gaya", "mrut", "maut", "???", "??????", "??"],
        "Swelling in Muscle": ["muscle", "maspeshi", "sujan", "swelling", "????????"],
        "Lameness (Hind Leg)": ["hind leg", "pichla pair", "hind", "????? ???"],
        "Crepitant Swelling": ["crepitant", "crackling", "awaaz", "awaaj", "?????", "????"],
        "Swollen Udder": ["udder", "than", "thanella", "swollen udder", "kasa", "??", "??", "???"],
        "Blood in Milk": ["blood", "khoon", "rakt", "red milk", "???", "????"],
        "Clots in Milk": ["clot", "tukde", "fata", "curd", "?????", "???"],
        "Painful Udder": ["pain", "dard", "dhukhta", "dukhne", "????", "?????"]
    };

    let matched = [];
    for (let sym of window.SYMPTOMS) {
        let keywords = synonymMap[sym] || [sym.toLowerCase()];
        for (let kw of keywords) {
            if (text.includes(kw.toLowerCase())) {
                matched.push(sym);
                break;
            }
        }
    }
    
    let checkboxes = document.querySelectorAll(".symcheck");
    checkboxes.forEach(cb => {
        if (matched.includes(cb.value)) {
            cb.checked = true;
        }
    });

    let vStatus = document.getElementById("voiceStatus");
    if (vStatus) {
        vStatus.innerText = matched.length > 0 ? ("Extracted Symptoms: " + matched.join(", ")) : "No specific symptoms matched from your description. Audio saved.";
    }
}
"""
    text = text.replace(old_func, new_func)
    with open('templates/index.html', 'w', encoding='utf-8') as f:
        f.write(text)
    print('Function updated successfully')
else:
    print('Failed to find function')
