# Setup

Voraussetzungen: Python ≥ 3.11, Git, OpenAI-API-Key.

## 1. Von GitHub holen und installieren

```powershell
git clone https://github.com/ra0037I/a2a-order-network.git
cd a2a-order-network
python -m venv .venv
.venv\Scripts\Activate.ps1        # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Konfigurieren

```powershell
copy .env.example .env            # Linux/macOS: cp .env.example .env
```

In der `.env` den eigenen Key eintragen:

```ini
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

## 3. Starten

**Terminal 1:** alle Dienste starten (Registry, MCP-Server und 4 Agents, jeweils als eigener Prozess)

```powershell
python run_all.py
```

Warten, bis `[run_all] ready` erscheint. Mit `Ctrl+C` wird alles gestoppt.

**Terminal 2:** Kunden-E-Mail ins Netzwerk schicken

```powershell
python client.py --demo 1         # 1, 2, 3 oder all
```

Das Angebot erscheint in der Konsole. Die Logs liegen unter `logs/` (`client_run_<zeit>.log` und eine Datei pro Agent).
Die registrierten Agents sieht man im Browser unter http://127.0.0.1:8000/agents.

Tests: `python -m pytest -q`

## Optional: über LiteLLM-Proxy

**Vorhandener Proxy:** in der `.env` Adresse und Key eintragen:

```ini
LLM_PROVIDER=litellm
LITELLM_API_BASE=http://<proxy-host>:4000
LITELLM_KEY=sk-<key-des-proxys>
```

**Kein Proxy verfügbar:** Ein eigener Proxy kann lokal installiert und gestartet werden.
`litellm[proxy]` ist bereits in `requirements.txt` enthalten. Der Proxy leitet an den OpenAI-Key aus der `.env` weiter.

1. Key erzeugen: `python -c "import secrets; print('sk-' + secrets.token_hex(24))"`
2. In der `.env` setzen (beide Keys identisch):
   ```ini
   LLM_PROVIDER=litellm
   LITELLM_API_BASE=http://localhost:4000
   LITELLM_KEY=sk-<key>
   LITELLM_MASTER_KEY=sk-<key>
   ```
3. Terminal 1: `python start_litellm.py`, Terminal 2: `python run_all.py`, Terminal 3: `python client.py --demo 1`

`run_all.py` prüft vor dem Start, ob der Proxy erreichbar ist und den Key akzeptiert.
Fehler `400 – No connected db`: `LITELLM_KEY` passt nicht zum Master-Key des laufenden Proxys.