# A2A Order Network: verteiltes Multi-Agent-System mit dynamischer Registry

**Industrial Computing, Homework Teil 2, Option A (Technical Track)**
MCI Master, Andreas Rofner
Das Projekt erweitert die Code-Beispiele der Vorlesung  MCI Industrial Computing mit Christian Maurer, *Sample 6 A2A* um ein verteiltes Multi-Agent-System mit dynamischer Agent-Registry und A2A-Kommunikation. Die Agents nutzen intern den MCP-Server aus Aufgabe 1 als Werkzeugkasten.
Ein verteiltes Multi-Agent-System für einen Industrieautomations-Händler. Es macht aus einer unstrukturierten **Kunden-E-Mail ein vollständiges Angebot**: Bestellung auslesen, jede Position mit kundenspezifischem Staffelrabatt bepreisen, Lager prüfen, Produktion für Fehlmengen planen und eine Freigaberegel anwenden.

Vier eigenständige Agents laufen jeweils als **eigener Prozess auf eigenem Port** und arbeiten über das **A2A-Protokoll** zusammen. Sie finden sich **zur Laufzeit über eine Agent-Registry**, keine Agent-Adresse ist im Code hinterlegt. Intern nutzt der Inventory-Agent den **MCP-Server aus Aufgabe 1** als Werkzeugkasten (*nach außen A2A, nach innen MCP*).

Technik wie in Sample 6 der Vorlesung: `pydantic-ai`-Agents, als A2A-Server bereitgestellt mit `fasta2a`, dazu eine eigene FastAPI-Registry.

## Schnellstart

Ausführliche Schritte: **[SETUP.md](SETUP.md)**

```powershell
git clone https://github.com/ra0037I/a2a-order-network.git
cd a2a-order-network
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env            # OPENAI_API_KEY eintragen
python run_all.py                 # Terminal 1
python client.py --demo 1         # Terminal 2
```

## Architektur

```
                         ┌───────────────────────────────────────────┐
                         │   Agent-Registry  :8000   (FastAPI)       │
                         │   Anmeldung · Heartbeat · Suche · Lookup  │
                         └───▲──────────▲──────────▲──────────▲──────┘
          (1) Lookup          │ Selbst-Anmeldung + Heartbeat (alle 15 s)
  client.py ────────────────►│          │          │          │
      │                       │          │          │          │
      │ (2) A2A message/send  │          │          │          │
      ▼                       │          │          │          │
 ┌──────────────────────┐     │          │          │          │
 │ orchestrator-agent   │─────┘          │          │          │
 │ :8100  (Root-Agent)  │  (3) find_agents(Fähigkeit) -> Suche in der Registry
 │ Tools: find_agents,  │  (4) ask_agent(Name) -> URL aus Registry -> A2A
 │ ask_agent, Freigabe  │                │          │          │
 └───┬────────┬────────┬┘                │          │          │
     │A2A     │A2A     │A2A              │          │          │
     ▼        ▼        ▼                 │          │          │
 ┌─────────┐ ┌──────────────────┐ ┌──────┴──────────┴──┐       │
 │ order-  │ │ inventory-       │ │ production-        │       │
 │ intake- │ │ pricing-agent    │ │ planning-agent     │───────┘
 │ agent   │ │ :8102            │ │ :8103              │
 │ :8101   │ │       │ MCP      │ │ Linienkapazität,   │
 │ CRM-    │ │       ▼ (HTTP)   │ │ Rückstand, QA →    │
 │ Lookup  │ │ MCP-Server :8200 │ │ Liefertermine      │
 └─────────┘ │ (Aufgabe 1)      │ └────────────────────┘
             └──────────────────┘
```

### Agents und Dienste

| Dienst | Port | Aufgabe | Eigene Tools |
|---|---|---|---|
| `registry` | 8000 | Verzeichnis der aktiven Agents. Holt und prüft die A2A-Agent-Card jedes Agents, verarbeitet Heartbeats und blendet Agents ohne Heartbeat nach 45 s aus. Suche exakt per Skill-ID oder per Stichwort (ohne Treffer: beste Kandidaten als Fallback) | – |
| `orchestrator-agent` | 8100 | Root-Agent: plant den Ablauf, sucht Sub-Agents, delegiert per A2A und schreibt das Angebot. Ein Ablauf-Guard im Code sichert die Reihenfolge ab | `find_agents`, `ask_agent`, `check_approval_policy` |
| `order-intake-agent` | 8101 | Freitext-E-Mail → strukturierte Bestellung als JSON (Kunde, Positionen, Mengen, Termin) | `lookup_customer` (Mini-CRM) |
| `inventory-pricing-agent` | 8102 | SKU-Zuordnung, Lagerprüfung, Staffelpreise, Audit-Log, alles **über MCP** | `search_products`, `price_line_item`, `log_quote_event` → MCP |
| `production-planning-agent` | 8103 | Produktionsaufträge für Fehlmengen mit Durchlaufzeit und Liefertermin (Arbeitstage) | `plan_production`, `ship_date_from_stock` |
| `mcp-inventory` | 8200 | MCP-Server aus Aufgabe 1 (Streamable HTTP) | `query_inventory`, `calculate_tiered_discount`, `append_audit_log` |

### Ablauf einer Anfrage

1. `client.py` fragt die Registry nach der Fähigkeit *„order to quote orchestration“* und bekommt `orchestrator-agent` mit seiner Adresse.
2. Der Client schickt die E-Mail per A2A `message/send` an den Orchestrator und fragt dann mit `tasks/get` nach, bis die Task abgeschlossen ist.
3. Das LLM des Orchestrators ruft `find_agents("order-extraction")` auf. Die Registry liefert den Agent, dessen Agent-Card diesen Skill anbietet: `order-intake-agent`. `ask_agent` holt die Adresse aus der Registry und delegiert die E-Mail per A2A. Zurück kommt eine validierte Bestellung inkl. Kundenstufe aus dem CRM.
4. Gleiches Muster für `stock-and-pricing`. Die validierte Bestellung wird automatisch angehängt. Der Inventory-Agent ruft die MCP-Tools des HW1-Servers auf.
5. Nur wenn das Preisergebnis Fehlmengen enthält, fragt der Orchestrator `production-scheduling` an (Preisergebnis wird angehängt).
6. `check_approval_policy`: nimmt die Nettosumme aus dem Preisergebnis. Angebote über 10.000 € werden als **PENDING APPROVAL by sales manager** markiert (Human-in-the-Loop).
7. Der Orchestrator schreibt das Angebot. Es geht als Artifact der A2A-Task an den Client zurück.

## LLM-Anbindung

Gesteuert über `LLM_PROVIDER` in der `.env`. Alle Agents nutzen dasselbe Backend.

| `LLM_PROVIDER` | Benötigte Einträge in `.env` | Hinweis |
|---|---|---|
| `openai` | `OPENAI_API_KEY`, `OPENAI_MODEL` | Standard, `gpt-4o-mini` |
| `litellm` | `LITELLM_API_BASE`, `LITELLM_KEY`, `LITELLM_DEFAULT_MODEL` | vorhandener Proxy oder eigener: `python start_litellm.py` (`LITELLM_KEY` = `LITELLM_MASTER_KEY`) |
| `azure` | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_KEY`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION` | Azure OpenAI / AI Foundry |
| `ollama` | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | lokal, Modell mit Tool-Calling |
| `mock` | – | regelbasierte Offline-Modelle zum Testen der Verkabelung, verstehen nur die Demo-E-Mails |

## Ausführen

```powershell
python run_all.py                 # Terminal 1: alle 6 Dienste als eigene Prozesse
python client.py --demo 1         # Terminal 2: Alpenwerk GmbH, gold, kleine Fehlmenge HMI → Produktion + Freigabe
python client.py --demo 2         #             Tiroler Anlagenbau AG, platinum, 500 Safety Relays → Deckel, große Fehlmenge
python client.py --demo 3         #             Bergbahn Service KG, kleine Bestellung, alles lagernd → keine Produktion
python client.py --demo all
python client.py --file meine_mail.txt
python -m pytest -q               # 12 Tests inkl. End-to-End-Lauf aller 6 Prozesse (Mock-LLM, auch mit „unfolgsamem“ LLM)
```

Einzelne Dienste lassen sich auch separat starten, z. B. in eigenen PyCharm-Run-Configurations:

```powershell
python -m registry.service
python mcp_inventory/server.py --http --port 8200
python -m agents.intake_agent
python -m agents.inventory_agent
python -m agents.production_agent
python -m agents.orchestrator_agent
```

Nützliche Adressen im laufenden Betrieb:

- http://127.0.0.1:8000/agents: Inhalt der Registry
- http://127.0.0.1:8000/docs: Registry-API (Swagger)
- http://127.0.0.1:8000/agents/search?skill=stock-and-pricing: Registry-Suche per Skill-ID
- http://127.0.0.1:8000/agents/search?q=production%20lead%20time: Registry-Suche per Stichwort
- http://127.0.0.1:8100/.well-known/agent-card.json: eine A2A-Agent-Card

## Logs

| Datei | Inhalt |
|---|---|
| `logs/client_run_<zeit>.log` | Anfrage, Registry-Lookup, A2A-Task-ID und -Status, fertiges Angebot |
| `logs/orchestrator-agent.log` | `[REGISTRY] find_agents`, `[A2A] -> / <-` pro Aufruf, `[GUARD]`, `[POLICY]` |
| `logs/registry.log` | `REGISTERED`, `SEARCH q=… -> Ranking`, Abmeldungen |
| `logs/order-intake-agent.log`, `logs/inventory-pricing-agent.log` (`[MCP] ->`), `logs/production-planning-agent.log` | Tool-Aufrufe pro Agent |
| `logs/mcp_inventory.log`, `mcp_inventory/data/audit.log` | MCP-Tool-Aufrufe und Audit-Trail |

## Projektstruktur

```
run_all.py                     startet alle Dienste als eigene Prozesse (prüft vorher den LiteLLM-Proxy)
start_litellm.py               startet den eigenen LiteLLM-Proxy mit den Keys aus .env
litellm_config.yaml            Konfiguration des eigenen LiteLLM-Proxys
client.py                      schickt Anfragen (Registry-Lookup → A2A)
registry/service.py            Agent-Registry (FastAPI)
agents/orchestrator_agent.py   Root-Agent: find_agents / ask_agent / Freigaberegel
agents/intake_agent.py         E-Mail → strukturierte Bestellung (+ CRM)
agents/inventory_agent.py      Lager + Preise über MCP
agents/production_agent.py     Produktionsplanung, Liefertermine
agents/models.py               strukturierte Ausgaben der Sub-Agents (Pydantic)
agents/mock_support.py         Hilfsfunktionen für die Offline-Mock-Modelle
common/a2a_client.py           A2A-Client (message/send + tasks/get)
common/a2a_server.py           pydantic-ai-Agent → A2A-Server (fasta2a) + Selbst-Anmeldung
common/registry_client.py      Anmelde-Schleife, Heartbeat, Suche, Lookup
common/llm.py                  LLM-Factory (openai | litellm | azure | ollama | mock)
common/settings.py             Ports, URLs, Schwellwerte (.env)
mcp_inventory/                 MCP-Server aus Aufgabe 1 (SQLite, Rabatt-Engine, Audit-Log)
demo_emails/                   3 Demo-Kunden-E-Mails
tests/
SETUP.md                       Installationsanleitung
```

## Designentscheidungen

- **Dynamische Suche statt fester Adressen:** Die einzige bekannte Adresse ist `REGISTRY_URL`. Agents melden sich beim Start selbst an, der Orchestrator wählt Sub-Agents zur Laufzeit nach Fähigkeit. Ein neuer Agent (z. B. Bonitätsprüfung) wird nutzbar, sobald er sich registriert. Am Orchestrator ändert sich nichts.
- **Die Registry vertraut der Agent-Card, nicht dem Aufrufer:** Agents schicken nur ihre URL. Die Registry holt `/.well-known/agent-card.json` selbst ab, so stimmen die gespeicherten Daten immer mit dem echten Endpunkt überein. Schreibende Aufrufe brauchen einen gemeinsamen `X-Registry-Key`.
- **Ablauf-Guard statt Vertrauen ins LLM:** Das LLM plant, der Code sichert ab. `find_agents` akzeptiert nur die drei Skill-IDs `order-extraction`, `stock-and-pricing`, `production-scheduling`, also kann das LLM nicht „falsch suchen“. `ask_agent` verweigert die Preisberechnung, solange keine validierte Bestellung mit CRM-Kundenstufe vorliegt, und hängt die Daten des Vorschritts selbst an. Eine Antwort wird erst akzeptiert, wenn alle nötigen Schritte gelaufen sind (Output-Validator → `ModelRetry`).
- **Strukturierte Ergebnisse:** Die Sub-Agents liefern Pydantic-Modelle (`agents/models.py`) statt Fließtext. Summen und Fehlmengen-Liste werden aus den Positionen neu berechnet und nicht vom LLM übernommen.
- **Lebenszeichen:** Heartbeat alle 15 s, Agents ohne Heartbeat seit 45 s werden ausgeblendet. Nach einem Neustart der Registry melden sich die Agents automatisch neu an.
- **MCP vs. A2A** („MCP gives AI its hands, A2A gives AI its colleagues“): Deterministische Aktionen (SQL, Rabattformel, Audit) sind MCP-Tools. Zielorientierte Teilaufgaben mit eigenem Denken sind A2A-Agents.
- **Human-in-the-Loop:** Ob ein Angebot freigegeben werden muss, entscheidet ein festes Policy-Tool, nicht das LLM. Die Schwelle ist über `APPROVAL_THRESHOLD_EUR` einstellbar.
- **Deterministische Zahlen:** Preise, Lagerstände, Mengen und Termine kommen ausschließlich aus Tool-Ergebnissen. Output-Validatoren übernehmen die Werte direkt aus den Tool-Aufrufen (Kundendaten aus dem CRM, Positionen aus `price_line_item`, Produktionsaufträge aus `plan_production` mit exakt der Fehlmenge). Das Angebot selbst erzeugt der Orchestrator im Code aus den geprüften Ergebnissen. Die LLMs routen und extrahieren, schreiben aber keine Zahlen.
