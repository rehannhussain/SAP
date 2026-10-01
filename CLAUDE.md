# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.
For the full functional reference (field-by-field mappings), see **`wfn.md`**.

## What this is

**ZWFN** — a SAP Fiori (SAPUI5 freestyle) shop-floor app for the Finishing
department's **Sanforizing (SNFR)** operation, backed by a Python Flask API.

Flow: the operator **scans a doff QR** → the screen shows the read-only batch
context → enters finishing values and runs a **machine start/stop timer** → on
**Save** the app (1) posts a **311 stock transfer 3019 → 3055**, (2) writes a
**header + detail** in SAP HANA, (3) marks the doff's transit record **Finished**,
and (4) mirrors the detail to a **KT SQL Server**.

Three backend systems:
- **SAP HANA** (`SAPHANADB`, user `ZMSQL`) via `hdbcli` — reads + the M/D inserts.
- **SAP (S/4HANA) RFC** (user `ZWFN_RFC`) via `pyrfc` — the 311 goods movement.
- **MS SQL Server** (`KT.SAP_FinishingDetail`) via `pyodbc` — a best-effort mirror.

Client `MANDT = 900` throughout.

## Run / develop

**No build step, no Node toolchain.** Python is the runtime and web server.

```powershell
# one-time
pip install -r server/requirements.txt          # flask, hdbcli, pyodbc, pyrfc
copy server\.env.example server\.env             # then fill in real credentials

# every time (kills stale servers, starts ONE HTTPS server, prints the URL)
.\run-https.ps1
```

- `run-https.ps1` is the sanctioned launcher. Starting servers by hand lets stale
  `app.py` processes pile up on port 8000 (Windows `SO_REUSEADDR`); an old
  **plain-HTTP** one then answers and the iPad camera breaks with "needs a secure
  connection". Always kill stale servers first (the script does).
- Manual run: `cd` to the repo, `$env:USE_HTTPS="true"; python server/app.py`.
  Confirm the console says **`Running on https://<lan-ip>:8000`**.
- No tests or linter. Validate DB-write changes with **insert/rollback** scripts in
  the scratchpad (never leave test rows); validate the 311 with **`SAP_RFC_MOCK=true`**
  — never post a real goods movement while testing.

## Architecture

**Single origin, no CORS.** `server/app.py` serves the SAPUI5 app from `../webapp`
at `/` *and* the JSON API under `/api/*`. The front end uses **relative** fetch
paths (`fetch("api/finishing/scan")`) — never hardcode an origin/port.

### Scan — `GET /api/finishing/scan?doff=<code>`
`ZWV_DOF_D` (A) ⋈ `ZWV_DOF_DD2` (B) ⋈ **`ZSTM_TRANSIT_D`** (C, on `DOFF_BATCHNO`).
- The transit join is a **filter**: only doffs with a live `ZSTM_TRANSIT_D` row
  (`STATUS='Doff in Transit'`, not reversed, deduped to the latest DOCID per doff)
  are scannable — i.e. currently in transit `3019 → 3055`. 404 otherwise.
- Value normalization (`_scan_forms`): first whitespace token, upper-cased. Tries an
  **exact** match (separators removed); if that misses and the code is separated
  (e.g. `261042-528-1446-01`, a QR that omits the `KT3L…` loom code), matches
  **`lot% + tail`** and accepts it **only when exactly one** batch matches (else 409).
- Returns batch context + `DD_BATCH_NO` (B.BATCH_NO), doff `DOFF_DOCID/_DTL`
  (B.DOCID/DOCID_DTL), and the transit context `TR_*` (WERKS/LGORT/UMLGO/MATNR/
  CHARG/MENGE/MEINS).

### Save — `POST /api/finishing/records`
Order matters (the 311 is authoritative):
0. **Check** unrestricted stock at `3019` for the batch (MATDOC net).
1. **Post the 311** for the entered **Finish Length** via `BAPI_GOODSMVT_CREATE`
   (GM_CODE 04, move type 311) + `BAPI_TRANSACTION_COMMIT`. **Fail ⇒ nothing else is
   written**; success ⇒ a material document number.
2. **HANA**: one shared `DOCID = MAX(DOCID over M and D)+1`; write the **header
   (`ZFN_FAB_PRD_M`) + detail (`ZFN_FAB_PRD_D`)** in a single transaction
   (retry once on a 301 key clash).
3. **Mark** the transit row `STATUS='Finished'` (+ material doc in `REVERSAL_REASON`).
4. **KT mirror** (`SAP_FinishingDetail`) best-effort — a failure only warns.

Key computed fields (full tables in `wfn.md`): `ARTICLE` drops the first 3 chars
(`FF HFZ-6514`→`HFZ-6514`); `TIMEUP` = elapsed **HHMMSS**, `TIMEMINUTES` = minutes;
`SHIFT` A/B/C from start time; `AUFNR/OUT_MATNR/OUT_UOM` from a ZSIZ_PLN ⋈ AFKO
lookup reading **MATDOC** (MSEG is empty here) + MARA; `Dye_Stop` from
`ZSIZE_PRD_M/D DYES_STOP='YES'`; `FINISH_TYPE` is a dropdown (FINISH/REFINISH/
LEADLINE) → **M header only** (D stays `FINISH`); `CHKSEL='X'` from the checkbox.

### Availability — `GET /api/health`
`SELECT 1 FROM DUMMY`. The UI shows a red banner + disables scan/save when HANA is
unreachable; short HANA timeouts (`HANA_TIMEOUT_MS`) fail fast.

### Front end (`webapp/`, namespace `finishing.sanfor`)
Plain `fetch()` (no OData). Compact single-screen iPad layout, theme `sap_horizon`:
branded header with a top-left logo, a prominent action toolbar (Start/Stop +
Save/Clear), machine times on their own line, Batch Details beside Finishing Entry.
Process ComboBox key=`MACHINE_WORKCEN` / text=`PROCESS_TYPE`. **Camera Scan QR**
decodes with vendored **jsQR** (`webapp/lib/jsQR.js`); Safari has no `BarcodeDetector`
and needs HTTPS for the camera. The Saved dialog shows the `DOCID` and the 311
material document.

## Config (`server/.env`, gitignored)
- HANA: `HANA_HOST/PORT/USER/PASSWORD`, `HANA_TIMEOUT_MS`.
- HTTPS: `USE_HTTPS`, optional `server/cert.pem`+`key.pem`.
- KT SQL Server: `KT_SERVER/DATABASE/UID/PWD/DRIVER`, `KT_ENABLED`.
- SAP RFC: `SAP_ASHOST/SYSNR/CLIENT/USER/PASSWD`, `SAP_GM_CODE`, `SAP_MOVE_TYPE`,
  `SAP_USE_MATERIAL_LONG`, `MOVE_311_ENABLED`, `SAP_RFC_MOCK`.

## Conventions & gotchas
- **UI5 runtime is hosted locally** at `webapp/resources/` (OpenUI5 **1.120.30**,
  ~540 MB, **gitignored**); `index.html` bootstraps `src="resources/sap-ui-core.js"`.
  Re-create after a fresh clone:
  ```bash
  curl -sL -o /tmp/ui5.zip https://github.com/SAP/openui5/releases/download/1.120.30/openui5-runtime-1.120.30.zip
  python -c "import zipfile; z=zipfile.ZipFile('/tmp/ui5.zip'); z.extractall('webapp', [n for n in z.namelist() if n.startswith('resources/') and not n.endswith('/')])"
  ```
- **`webapp/index.html` has a load-bearing height fix** (`html,body,#content` +
  `#content .sapUiView` at `height:100%`); removing it renders the page blank. It also
  carries the running-animation keyframes and `.finActionBar`/`.finTimesBar` styling.
- **UI5 caches XML views.** Hard-refresh after editing a `.view.xml`. **XML comments
  must not contain `--`** (double hyphen) or the view fails to parse.
- The `Component-preload.js` 404 is expected (no optimized UI5 build).
- **Logo** `webapp/img/logo.svg` is a placeholder — swap for the licensed asset.
- **Secrets** live only in `server/.env` (gitignored); keep `.env.example` as
  placeholders. `cert.pem`/`key.pem` and `webapp/resources/` are gitignored too.
- **Stock lives in MATDOC, not MSEG/MARD/MCHB** on this S/4HANA — MSEG is empty and
  the aggregates are 0. Read stock from `MATDOC`. Move type 311 is posted via BAPI,
  never by writing MARD/MCHB/MSEG. Numeric materials are ALPHA-padded to 18 (or
  `MATERIAL_LONG` when >18). `ENTRY_QNT` must be a **Decimal** for pyrfc.
- **Cross-system writes aren't one transaction:** 311 posts first and is authoritative;
  if the HANA save then fails, the error returned includes the material document
  number so the movement can be reconciled.
- **HANA grants:** `ZMSQL` needs INSERT on **both** `ZFN_FAB_PRD_M` and
  `ZFN_FAB_PRD_D` (`GRANT INSERT ON SAPHANADB.<table> TO ZMSQL;`).
- **pyrfc** needs the SAP NW RFC SDK (`SAPNWRFC_HOME`, default `C:\SAP\nwrfcsdk`);
  `_register_nwrfc_sdk()` adds its `lib` to the DLL path on Windows.
