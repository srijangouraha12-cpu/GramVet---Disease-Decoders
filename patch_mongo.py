with open('app.py', 'r', encoding='utf-8') as f:
    text = f.read()

old_mongo = '"audio_url": audio_url,\n        "reported_at": reported_at,'
new_mongo = '"audio_url": audio_url,\n        "audio_data": d.get("audio_base64"),\n        "reported_at": reported_at,'

if old_mongo in text:
    text = text.replace(old_mongo, new_mongo)
    with open('app.py', 'w', encoding='utf-8') as f:
        f.write(text)
    print('Updated successfully')
else:
    print('Could not find string')
