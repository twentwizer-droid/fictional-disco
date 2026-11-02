# desk — Grok Bot trading desk (Windows PC)

Finds, audits, scores and sizes freshly graduated pump.fun coins and sends you a button.
It never places orders and never holds keys to your funds: you buy and sell in Axiom yourself.

## Run the STEP 2 probe (once)

In PowerShell, from the repo folder:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r desk\requirements.txt
copy desk\.env.example desk\.env
notepad desk\.env          # fill in what you have, leave the rest empty
python desk\probe.py
```

Then send `desk\logs\raw\REPORT.txt` back. It contains no keys.
