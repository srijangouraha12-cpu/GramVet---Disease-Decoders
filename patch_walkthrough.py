with open('C:\\Users\\Srijan\\.gemini\\antigravity\\brain\\cd89819b-a07d-4a6d-bc61-3f7dccf63a85\\walkthrough.md', 'r', encoding='utf-8') as f:
    text = f.read()

old_text = "The frontend converts the audio recording to `base64` and sends it along with the report to `/api/cases/report`. The backend decodes and saves it as a `.webm` file in the `/media/` folder and logs the path."
new_text = "The frontend converts the audio recording to `base64` and sends it along with the report to `/api/cases/report`. The backend decodes and saves it as a `.webm` file in the `/media/` folder. It also stores the raw base64 audio data in the MongoDB live server by inserting it into the `health_reports` collection."

text = text.replace(old_text, new_text)

with open('C:\\Users\\Srijan\\.gemini\\antigravity\\brain\\cd89819b-a07d-4a6d-bc61-3f7dccf63a85\\walkthrough.md', 'w', encoding='utf-8') as f:
    f.write(text)
