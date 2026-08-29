# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this is

**ZWFN** — a SAP Fiori (SAPUI5 freestyle) shop-floor entry app for the **Finishing
department's Sanforizing (SNFR)** operation, backed by a small Python Flask API that talks
to SAP HANA (schema `SAPHANADB`) via `hdbcli`.

Flow: the operator **scans a doff QR** (`DOFF_BATCHNO`) → the screen shows the read-only
production context → the operator enters finishing values and runs a **machine start/stop
timer** → on Save one row is inserted into `SAPHANADB.ZFN_FAB_PRD_D`.

This app was migrated out of the ZMDASHBOARD2 meter-reading project into its own repo.

## Run / develop

**No build step, no Node toolchain.** Python is the runtime and the web server.

```bash
pip install -r server/requirements.txt      # flask + hdbcli
cp server/.env.example server/.env           # then fill in real HANA credentials
python server/app.py                         # serves UI + API at http://localhost:8000
```

- `PORT` (env / `.env`) changes the port; default `8000`.
- Flask runs with `debug=True` (Werkzeug reloader → two python processes). When
  restarting, make sure **port 8000 is free**; kill lingering `server/app.py` processes.
- No tests or linter configured.

## Architecture

**Single origin, no CORS.** `server/app.py` serves the static SAPUI5 app from `../webapp`
at `/` *and* the JSON API under `/api/*`. The front end uses **relative** fetch paths
(`fetch("api/finishing/scan")`) — never hardcode an origin/port.

**Back end** (`server/app.py`, Flask + `hdbcli`), two endpoints:
- `GET /api/finishing/scan?doff=<DOFF_BATCHNO>` — parameterized join of `ZWV_DOF_D` /
  `ZWV_DOF_DD2` (`MANDT='900'`); returns the single batch row or `404`.
- `POST /api/finishing/records` — inserts one run into `ZFN_FAB_PRD_D`. Server generates
  `DOCID = MAX(TO_BIGINT(DOCID))+1` (guarded against non-numeric ids, `MANDT='900'`),
  zero-padded to 10, retrying once on a unique-key clash (errorcode 301). Machine duration
  is recomputed server-side from the client start/stop timestamps: `TIMEUP` = total
  **seconds**, `TIMEMINUTES` = total **minutes**. Fixed values: `OPERATION='SNFR'`,
  `FINISH_TYPE='FINISH'`, and the `ZWFN` TCODE stored in `REMARKS` (there is no TCODE
  column). `USERIN`/`USERUP`/`OPERATOR` = the entered operator. Only ~32 business columns
  are listed; every other column of the table is NOT NULL but has a DB default.

**Front end** (`webapp/`, namespace `finishing.sanfor`) — plain `fetch()` (no OData):
- `index.html` → `Component.js` → `manifest.json` (`rootView` = `view/FinishingForm`).
- Compact single-screen iPad layout, theme `sap_horizon`: a branded header with a
  **top-left logo**, a **prominent** action toolbar (Start/Stop + machine times +
  Save/Clear), and **Batch Details beside Finishing Entry**. Verified no-scroll at
  1024×768 and 768×1024.
- One ComboBox drives two columns: its **key = `MACHINE_WORKCEN`** code and its
  **text = `PROCESS_TYPE`** name (`ZMUF_01`=MUZZI, `ZMSN_01`=MORRISON, `ZCIS_01`=MONFORT,
  `ZRFS_01`=CIBITEX SANFOR).
- **Camera QR scan (iPad):** a *Scan QR* button opens the rear camera in a dialog and
  decodes with **jsQR** (vendored locally at `webapp/lib/jsQR.js`, MIT, loaded via a plain
  `<script>` in `index.html`; Safari has no `BarcodeDetector`). The controller draws video
  frames to a canvas and runs `jsQR` per `requestAnimationFrame`; on a hit it fills the
  scan field and runs the normal lookup. Camera errors (permission/insecure context) are
  caught and surfaced as a MessageBox — no crash, and the handheld-scanner / type path
  still works.

## HTTPS (required for the iPad camera)

Safari blocks `getUserMedia` over plain `http://` on a LAN IP, so the camera scan only
works over HTTPS. Run with `USE_HTTPS=true` (env or `.env`): `server/app.py` uses
`server/cert.pem` + `server/key.pem` if present, else a throwaway **adhoc** cert
(needs the `cryptography` package; a new cert each restart, so the iPad re-accepts the
warning every time). For a stable cert including your LAN IP:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout server/key.pem -out server/cert.pem -subj "/CN=zwfn-finishing" \
  -addext "subjectAltName=IP:<YOUR_LAN_IP>,IP:127.0.0.1,DNS:localhost"
```

Then on the iPad open `https://<host-lan-ip>:8000`, accept the self-signed warning once,
and allow camera access. `cert.pem`/`key.pem` are gitignored.

## Conventions & gotchas

- **UI5 runtime is hosted locally** at `webapp/resources/` (OpenUI5 **1.120.30**), served by
  Flask, so `index.html` bootstraps from `src="resources/sap-ui-core.js"`. This loads at LAN
  speed and needs **no internet** — first load ~1 s on the LAN (~8 MB uncached) vs many
  seconds from the public CDN. `webapp/resources/` is **gitignored** (~540 MB, third-party,
  not source). To fall back to the CDN, set the bootstrap `src` to
  `https://sdk.openui5.org/1.120.30/resources/sap-ui-core.js` (only specific 1.120.x patches
  are hosted — `.28`/`.30`, not `.0`).
  **Re-create the local runtime** (after a fresh clone or a version bump):
  ```bash
  curl -sL -o /tmp/ui5.zip https://github.com/SAP/openui5/releases/download/1.120.30/openui5-runtime-1.120.30.zip
  python -c "import zipfile; z=zipfile.ZipFile('/tmp/ui5.zip'); z.extractall('webapp', [n for n in z.namelist() if n.startswith('resources/') and not n.endswith('/')])"
  ```
- **`webapp/index.html` has a load-bearing height fix** (`html,body,#content` +
  `#content .sapUiView` at `height:100%` plus `data-height="100%"`); removing it renders
  the page blank. It also carries the machine-running keyframes and the `.finActionBar`
  toolbar styling (uses SAP theme CSS vars with hex fallbacks).
- **Logo is a placeholder** — `webapp/img/logo.svg`. Replace it with the licensed
  SAP/company asset (same path, or repoint the `Image` src in the view).
- **UI5 caches XML views aggressively.** After editing a `.view.xml`, hard-refresh
  (Ctrl+F5). XML comments must not contain `--` (double hyphen) or the view fails to parse.
- The `Component-preload.js` 404 in the console is expected (no optimized UI5 build).
- **Secrets:** only `server/.env` (gitignored) holds real credentials; keep
  `server/.env.example` as placeholders.
- **HANA write grant:** the `ZMSQL` DB user has schema-wide SELECT but per-table INSERT.
  Inserts into `ZFN_FAB_PRD_D` fail with HANA **error 258 "insufficient privilege"** until
  a DBA runs `GRANT INSERT ON SAPHANADB.ZFN_FAB_PRD_D TO ZMSQL;`.
