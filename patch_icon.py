with open('templates/index.html', 'r', encoding='utf-8') as f:
    text = f.read()

old_btn = '&#127904;'
new_btn = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align: middle; margin-top:-2px;"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2"></path><line x1="12" y1="19" x2="12" y2="23"></line><line x1="8" y1="23" x2="16" y2="23"></line></svg>'

if old_btn in text:
    text = text.replace(old_btn, new_btn)
else:
    print('Failed to find mic icon')

# The function resets it to &#127904; as well in stopSymptomVoiceRecording, let's fix that
old_reset = 'btn.innerHTML = "&#127904;";'
new_reset = 'btn.innerHTML = \'<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align: middle; margin-top:-2px;"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2"></path><line x1="12" y1="19" x2="12" y2="23"></line><line x1="8" y1="23" x2="16" y2="23"></line></svg>\';'
if old_reset in text:
    text = text.replace(old_reset, new_reset)

# Also replace the red flag emoji &#128681; with a Stop SVG
old_stop = 'btn.innerHTML = "&#128681;";'
new_stop = 'btn.innerHTML = \'<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align: middle; margin-top:-2px;"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect></svg>\';'
if old_stop in text:
    text = text.replace(old_stop, new_stop)

with open('templates/index.html', 'w', encoding='utf-8') as f:
    f.write(text)
print('UI icons patched')
