import re
with open("templates/index.html", "r", encoding="utf-8") as f:
    text = f.read()

start_idx = text.find('function matchSymptomsFromTranscript')
end_idx = text.find('</script>', start_idx)

new_func = """async function matchSymptomsFromTranscript(text) {
    let vStatus = document.getElementById("voiceStatus");
    if (vStatus) vStatus.innerText = "Processing text with AI / Extractor...";
    
    try {
        let resp = await api("/api/extract_symptoms", {
            method: "POST",
            body: JSON.stringify({text: text})
        });
        
        let matched = resp.symptoms || [];
        
        let checkboxes = document.querySelectorAll(".symcheck");
        checkboxes.forEach(cb => {
            if (matched.includes(cb.value)) {
                cb.checked = true;
            }
        });

        if (vStatus) {
            vStatus.innerText = matched.length > 0 ? ("Extracted: " + matched.join(", ")) : "No exact symptoms matched from description. Audio saved.";
        }
    } catch(e) {
        console.error("Extraction error", e);
        if (vStatus) vStatus.innerText = "Error extracting symptoms. Audio saved.";
    }
}
"""

if start_idx != -1 and end_idx != -1:
    text = text[:start_idx] + new_func + text[end_idx:]
    with open("templates/index.html", "w", encoding="utf-8") as f:
        f.write(text)
    print("UI JS updated")
else:
    print("Failed to find JS function")
