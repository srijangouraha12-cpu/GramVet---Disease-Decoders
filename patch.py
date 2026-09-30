import re

with open('templates/index.html', 'r', encoding='utf-8') as f:
    html = f.read()

old_toolbar = '<h3>${t("symptoms", "Symptoms")}</h3><span class="muted2">Select all observed</span></div>${symptomGrid()}'
new_toolbar = '''<h3>${t("symptoms", "Symptoms")} <button id="micBtn" class="btn secondary" style="border-radius:50%;padding:4px 8px;font-size:16px" type="button" onclick="startSymptomVoiceRecording(this)" title="Record Symptoms Voice">&#127904;</button></h3><span class="muted2">Select all observed or tap mic</span></div>
<div id="voiceStatus" style="font-size:12px;color:var(--green);display:none;margin-bottom:8px;">Listening... speak symptoms.</div>
<div id="voiceTranscript" style="font-size:13px;padding:8px;background:var(--soft);border-radius:8px;margin-bottom:10px;display:none;"></div>
${symptomGrid()}'''
html = html.replace(old_toolbar, new_toolbar)

old_submit = 'longitude:window.reportLon,...reportInputs})});'
new_submit = 'longitude:window.reportLon,audio_base64:window._lastAudioBase64,...reportInputs})});'
html = html.replace(old_submit, new_submit)

old_reset = 'window.reportLat=null; window.reportLon=null; window._reportBusy=false;'
new_reset = 'window.reportLat=null; window.reportLon=null; window._reportBusy=false; window._lastAudioBase64=null;'
html = html.replace(old_reset, new_reset)

old_timeline_item = 'return `<div class="timeline-item"><b>${t("m_report")}  ${esc(r.reported_at.replace("T"," ").slice(0,16))}</b><div class="muted">${esc(symTranslated)}</div>${r.notes?`<div>${esc(r.notes)}</div>`:""}${inputLine}${modelLine}${wxLine}</div>`;'
new_timeline_item = 'let audioLine = r.audio_url ? `<div style="margin-top:5px;"><audio controls src="${r.audio_url}" style="height:30px;"></audio></div>` : "";\n        return `<div class="timeline-item"><b>${t("m_report")}  ${esc(r.reported_at.replace("T"," ").slice(0,16))}</b><div class="muted">${esc(symTranslated)}</div>${r.notes?`<div>${esc(r.notes)}</div>`:""}${inputLine}${modelLine}${wxLine}${audioLine}</div>`;'
html = html.replace(old_timeline_item, new_timeline_item)

with open('templates/index.html', 'w', encoding='utf-8') as f:
    f.write(html)
print('Patched successfully')
