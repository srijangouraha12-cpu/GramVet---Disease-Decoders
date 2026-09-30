# GramVet Exotel IVR Integration

The IVR integration is isolated in `app.py` and does not replace the existing web reporting route.

## Exotel Passthru URLs

Use the current public HTTPS ngrok URL in place of `NGROK_BASE`.

- Pincode: `NGROK_BASE/ivr/exotel/passthru?stage=pincode`
- Animal ID: `NGROK_BASE/ivr/exotel/passthru?stage=animal_id`
- Symptoms: `NGROK_BASE/ivr/exotel/passthru?stage=symptom`

Use GET and synchronous Passthru (Async OFF). The 200 branch continues the Exotel flow.

## Final IVR flow

Farmer call -> report sick animal -> pincode -> existing animal ID -> species -> gender -> vaccinated -> vaccine (one selection) -> symptoms -> thank-you -> hangup.

The backend never creates an animal from IVR. `CallFrom` identifies the registered farmer, and the entered animal ID must belong to that farmer.

Symptoms 1-5 are accepted as multiple digits in one Gather (for example `134#`):

1. Fever / High Body Temperature
2. Cough
3. Difficulty Breathing
4. Reduced Appetite
5. Diarrhea

The final symptom Passthru creates the case using the same disease ML, outbreak, triage, escalation, notification, SQLite and MongoDB-sync pipeline used by the existing application reporting flow.

## Run

Start the Flask app as usual:

```powershell
python app.py
```

Then expose port 5000 with ngrok:

```powershell
C:\ngrok.exe http 5000
```

Update the three Exotel Passthru URLs whenever the ngrok hostname changes.
