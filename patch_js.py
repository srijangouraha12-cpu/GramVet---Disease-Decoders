with open('templates/index.html', 'r', encoding='utf-8') as f:
    html = f.read()

js_code = """
let mediaRecorder = null;
let audioChunks = [];
let speechRec = null;
let silenceTimer = null;
let finalTranscript = "";

function startSymptomVoiceRecording(btn) {
    if (mediaRecorder && mediaRecorder.state === "recording") {
        stopSymptomVoiceRecording();
        return;
    }
    
    let lang = document.getElementById("langApp").value || "en";
    let speechLang = "en-IN";
    if (lang === "hi") speechLang = "hi-IN";
    if (lang === "mr") speechLang = "mr-IN";

    navigator.mediaDevices.getUserMedia({ audio: true }).then(stream => {
        mediaRecorder = new MediaRecorder(stream);
        audioChunks = [];
        mediaRecorder.ondataavailable = e => { if (e.data.size > 0) audioChunks.push(e.data); };
        mediaRecorder.onstop = () => {
            let blob = new Blob(audioChunks, { type: "audio/webm" });
            let reader = new FileReader();
            reader.readAsDataURL(blob);
            reader.onloadend = () => {
                window._lastAudioBase64 = reader.result;
                document.getElementById("voiceStatus").innerText = "Audio recorded! Matching symptoms...";
                matchSymptomsFromTranscript(finalTranscript);
            }
        };

        let vStatus = document.getElementById("voiceStatus");
        let vText = document.getElementById("voiceTranscript");
        if (vStatus) { vStatus.style.display = "block"; vStatus.innerText = "Listening... Speak your symptoms."; }
        if (vText) { vText.style.display = "block"; vText.innerText = ""; }
        finalTranscript = "";
        
        btn.innerHTML = "&#128681;"; // red stop or something
        btn.style.background = "var(--red)";

        let SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (SpeechRecognition) {
            speechRec = new SpeechRecognition();
            speechRec.continuous = true;
            speechRec.interimResults = true;
            speechRec.lang = speechLang;

            speechRec.onresult = function(event) {
                clearTimeout(silenceTimer);
                
                let interim = "";
                finalTranscript = "";
                for (let i = 0; i < event.results.length; ++i) {
                    if (event.results[i].isFinal) finalTranscript += event.results[i][0].transcript;
                    else interim += event.results[i][0].transcript;
                }
                if (vText) vText.innerText = finalTranscript + " " + interim;

                silenceTimer = setTimeout(() => { stopSymptomVoiceRecording(); }, 3000);
            };
            
            speechRec.onend = function() {};
            speechRec.start();
            silenceTimer = setTimeout(() => { stopSymptomVoiceRecording(); }, 3000);
        } else {
            if (vText) vText.innerText = "Speech recognition not supported in this browser. Audio will be saved.";
            silenceTimer = setTimeout(() => stopSymptomVoiceRecording(), 5000);
        }

        mediaRecorder.start();
    }).catch(err => {
        alert("Microphone access denied or error: " + err);
    });
}

function stopSymptomVoiceRecording() {
    clearTimeout(silenceTimer);
    if (mediaRecorder && mediaRecorder.state === "recording") {
        mediaRecorder.stop();
        mediaRecorder.stream.getTracks().forEach(t => t.stop());
    }
    if (speechRec) {
        try { speechRec.stop(); } catch(e){}
    }
    let btn = document.getElementById("micBtn");
    if (btn) {
        btn.innerHTML = "&#127904;";
        btn.style.background = "";
    }
}

function matchSymptomsFromTranscript(text) {
    text = text.toLowerCase();
    
    const synonymMap = {
        "Fever": ["fever", "bukhar", "taap", "garam", "hot"],
        "Loss of Appetite": ["appetite", "bhook", "not eating", "kha nahi", "khana", "jevan", "chara"],
        "Lethargy": ["lethargy", "weak", "kamzor", "thaka", "ashakt", "sust"],
        "Reduced Milk Yield": ["milk", "doodh", "dudh", "kam doodh", "yield"],
        "Lesions/Blisters on Mouth": ["blister", "mouth", "muh", "chhale", "lesion", "ulcer", "tond"],
        "Lesions/Blisters on Feet": ["feet", "foot", "pair", "khur", "chhale", "blister", "paya"],
        "Lameness": ["lame", "limp", "langda", "langdana", "chalne me dikkat"],
        "Excessive Salivation": ["saliva", "drool", "lar", "laar", "thook", "gala"],
        "Skin Nodules/Lumps": ["nodule", "lump", "bump", "gath", "gaath", "sujan skin"],
        "Swollen Lymph Nodes": ["lymph", "node", "gland", "sujan gland"],
        "High Fever": ["high fever", "tez bukhar", "kadak taap"],
        "Sudden Death": ["death", "dead", "mar gaya", "mrut", "maut"],
        "Swelling in Muscle": ["muscle swelling", "maspeshi sujan", "sujan", "swelling"],
        "Lameness (Hind Leg)": ["hind leg", "pichla pair", "hind", "lame"],
        "Crepitant Swelling": ["crepitant", "crackling", "awaaz", "awaaj"],
        "Swollen Udder": ["udder", "than", "thanella", "swollen udder", "kasa"],
        "Blood in Milk": ["blood", "khoon", "rakt", "red milk"],
        "Clots in Milk": ["clot", "tukde", "fata", "curd"],
        "Painful Udder": ["pain", "dard", "dhukhta", "dukhne"]
    };

    let matched = [];
    for (let sym of window.SYMPTOMS) {
        let keywords = synonymMap[sym] || [sym.toLowerCase()];
        for (let kw of keywords) {
            if (text.includes(kw)) {
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
        vStatus.innerText = matched.length > 0 ? ("Matched: " + matched.join(", ")) : "No exact symptoms matched. Audio saved.";
    }
}
</script>"""

html = html.replace('</script>', js_code)

with open('templates/index.html', 'w', encoding='utf-8') as f:
    f.write(html)
print('JS functions added')
