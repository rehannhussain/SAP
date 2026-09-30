# ZWFN — Finishing (Sanforizing) Entry

Shop-floor app for the Finishing department's **Sanforizing (SNFR)** operation.
An operator scans a doff QR, reviews the batch, runs a machine start/stop timer,
and on **Save** the run is written to SAP HANA (a header + a detail row) and
mirrored to a KT SQL Server.

- **Front end:** SAPUI5 (freestyle, `sap_horizon`), served by Flask. iPad-first,
  single screen, with a camera **Scan QR** button (HTTPS only).
- **Back end:** Python Flask + `hdbcli` (HANA) + `pyodbc` (SQL Server).
- **Client:** `MANDT = 900` throughout.

---

## Operator workflow

1. **Scan** the doff QR (or type it / use a handheld scanner).
2. **Batch Details** fill in read-only (article, dyeset, lot, beams, doff batch, …).
3. Enter **Operator, Palate No, Batcher No, Finish Length**, pick the **Process /
   Machine** and the **Finish Type**, optionally tick **CHKSEL**.
4. **Start Machine** → live timer + running animation; **Stop Machine** → total time.
5. **Save** → writes the records and shows the new `DOCID`.

---

## Scan lookup (`GET /api/finishing/scan?doff=<code>`)

Joins `SAPHANADB.ZWV_DOF_D` (A) ⋈ `ZWV_DOF_DD2` (B) on `DOCID`, `MANDT = 900`.

- The value is cleaned: keep the first whitespace token (drops a trailing label
  like `TRIAL`), upper-cased; separators kept for pattern matching.
- **Exact** match first (all separators removed).
- If that misses and the code is separated (e.g. `261042-528-1446-01`, a QR that
  omits the `KT3L…` loom code baked into `DOFF_BATCHNO`), match **`lot% + tail`**
  (`261042%528144601`) and accept **only when exactly one** batch matches (else
  `409` — never a wrong guess).
- Returns the batch context plus `DD_BATCH_NO` (= `B.BATCH_NO`) and the doff
  `DOFF_DOCID` / `DOFF_DOCID_DTL` (= `B.DOCID` / `B.DOCID_DTL`).

---

## Save (`POST /api/finishing/records`)

Generates **one shared `DOCID`** = `MAX(DOCID over M and D) + 1`, zero-padded to 10,
and writes the **header + detail in a single HANA transaction** (both or neither;
retries once on a unique-key clash). Then **best-effort** mirrors to SQL Server.

Computed values:
- `DOCID_DTL` = next line seq for the DOCID → `0000000001` for a new DOCID.
- `ARTICLE` = query article with the first 3 chars dropped (`FF HFZ-6514` → `HFZ-6514`).
- `TIMEUP` = elapsed duration **HHMMSS** (e.g. `164455`); `TIMEMINUTES` = minutes.
- `SHIFT` from machine start time: **A** 07:00–15:00, **B** 15:00–23:00, else **C**.
- `AUFNR` / `OUT_MATNR` / `OUT_UOM` — order lookup (see below).
- `Dye_Stop` — SIZING lookup (see below).

### 1. Header — `SAPHANADB.ZFN_FAB_PRD_M`

| Column | Value |
|---|---|
| `DOCID` | shared id |
| `DYESET` | A.DYESET |
| `ARTICLE` | stripped article |
| `LOTNO_SNR` | A.LOT_NO |
| `BEAM_NO_SRN` | A.BEAM_NO |
| `LEGACY_NO_SNR` | A.LEGACY_NO |
| `DOC_DATE` | current date |
| `PROCESS_TYPE` | `FINISH` (fixed) |
| `MACHINE_WORKCEN` | dropdown value (`ZMUF_01`/`ZMSN_01`/`ZCIS_01`/`ZRFS_01`) |
| `OPERATION` | `SNFR` (fixed) |
| `FINISH_TYPE` | dropdown (`FINISH`/`REFINISH`/`LEADLINE`) — **header only** |
| `AUFNR`, `OUT_MATNR`, `OUT_UOM` | order lookup |
| `DATEIN`/`TIMEIN`/`USERIN` | start date/time + operator |
| `DATEUP`/`TIMEUP`/`USERUP` | stop date + elapsed HHMMSS + operator |
| `TCODE` | `ZWFN` |

### 2. Detail — `SAPHANADB.ZFN_FAB_PRD_D`

Same `DOCID` as the header, `DOCID_DTL` = line seq. Key mappings:

| Column | Value |
|---|---|
| `DYESET_CD` | A.DYESET · `ARTICLE` stripped · `LOTNO` A.LOT_NO |
| `BEAM_NO` | A.BEAM_NO |
| `LEGACY_NO` | A.LEGACY_NO |
| `OPERATOR`/`USERIN`/`USERUP` | operator |
| `PALLATE_NO`/`BATCHER_NO`/`FINISH_LENGTH` | inputs |
| `PROCESS_TYPE` | dropdown text (e.g. `MORRISON SANFOR`) |
| `MACHINE_WORKCEN` | dropdown value |
| `OPERATION` | `SNFR` · `FINISH_TYPE` `FINISH` (fixed) · `REMARKS` `ZWFN` |
| `DATEIN`/`TIMEIN`/`START_DATE`/`START_TIME` | start |
| `DATEUP`/`STOP_DATE`/`STOP_TIME` | stop · `TIMEUP` elapsed HHMMSS · `TIMEMINUTES` |
| `BATCH_NO` | A.BATCH_NO |
| `DOFF_BATCH_NO` | B.BATCH_NO (`DD_BATCH_NO`) |
| `DOFF_DOCID`/`DOFF_DOCID_DTL` | B.DOCID / B.DOCID_DTL |
| `SALES_ORDER_NO`/`LOOM_NO`/`DOFF_LENGTH` | from scan |
| `CHKSEL` | `X` if checkbox ticked, else blank |
| `SHIFT` | A/B/C from start time |

### 3. KT SQL Server mirror — `KT.SAP_FinishingDetail` (best-effort)

Upsert by `(DOCID, DOCID_DTL)` — UPDATE if present, else INSERT. A failure here
**never** undoes the HANA save; the operator sees a warning instead.

| Column | Value |
|---|---|
| `Article` | stripped article |
| `LotNo`/`LoomNo` | A.LOT_NO / A.LOOM_NO |
| `BeamNo` | **A.LEGACY_NO** (per the ABAP) |
| `Fabric_Code` | `dbo.Func_FAB_WeavingCode_FabricCode(<article>)` |
| `DOCID`/`DOCID_DTL`/`PalletNo` | id + palate |
| `Dyeing_Code` | A.DYESET |
| `Dye_Stop` | SIZING lookup → 1/0 |
| `Finish_Length`/`DOFF_BATCH_NO` | input / B.BATCH_NO |
| `REFINISH_DOCID` | blank |
| `PRDDATE` | current date (`CONVERT(datetime, …, 112)`) |

---

## Lookups

**Order (AUFNR / OUT_MATNR / OUT_UOM)** — mirrors the ABAP, reading **MATDOC**
(MSEG is empty on this S/4HANA):

```
ZSIZ_PLN_M ⋈ ZSIZ_PLN_D ⋈ ZSIZPLN_WF_ORD_D ⋈ AFKO
 where LOT_NO = <lot>
   and LOT_NO in (select ABLAD from MATDOC
                  where MATNR like '0000000036%' and BWART='101' and AUFNR like '0060%')
→ AUFNR = FINISHING_PROD_NO, OUT_MATNR = AFKO.PLNBEZ
→ OUT_UOM = MARA.MEINS for OUT_MATNR
```
Returns blanks when nothing matches (SELECT-SINGLE semantics).

**Dye_Stop (DYE_CHK)** — from SIZING: `ZSIZE_PRD_M ⋈ ZSIZE_PRD_D` where
`LOT_NO = <lot>` and `LEGACY_NO = <legacy>`; `DYES_STOP = 'YES'` → 1, else 0.

---

## Running the app

```powershell
.\run-https.ps1          # stops stale servers, starts ONE HTTPS server, prints the URL
```
Or manually:
```powershell
cd D:\Projects\SAP\ZWFN
$env:USE_HTTPS="true"; python server/app.py
```
Then open **`https://<pc-lan-ip>:8000`** on the iPad, accept the self-signed
certificate once, and tap **Scan QR** (the camera needs HTTPS; the handheld
scanner / typing work over http too).

### Config (`server/.env`, gitignored)
- `HANA_HOST/PORT/USER/PASSWORD`, `HANA_TIMEOUT_MS`
- `USE_HTTPS`, optional `server/cert.pem`+`key.pem`
- `KT_SERVER/DATABASE/UID/PWD/DRIVER`, `KT_ENABLED`

### Prerequisites / grants
- `pip install -r server/requirements.txt` (flask, hdbcli, **pyodbc**) and the
  **ODBC Driver 17 for SQL Server**.
- Re-create the local UI5 runtime after a fresh clone (see `CLAUDE.md`).
- HANA user needs **INSERT on both** `ZFN_FAB_PRD_M` and `ZFN_FAB_PRD_D`:
  ```sql
  GRANT INSERT ON SAPHANADB.ZFN_FAB_PRD_M TO ZMSQL;
  GRANT INSERT ON SAPHANADB.ZFN_FAB_PRD_D TO ZMSQL;
  ```

See `CLAUDE.md` for architecture details and gotchas.
