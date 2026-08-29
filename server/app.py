"""
Flask backend for the ZWFN Finishing (Sanforizing) entry app.

Serves the SAPUI5 (Fiori) app from ../webapp at / and exposes a small JSON API:

  GET  /api/finishing/scan?doff=<DOFF_BATCHNO>  -> read-only doff production context
  POST /api/finishing/records                   -> insert one finished run

Data lives in SAP HANA (schema SAPHANADB) and is reached via hdbcli. The scan reads
ZWV_DOF_D joined to ZWV_DOF_DD2; the insert writes ZFN_FAB_PRD_D. Client MANDT is '900'.

Configuration comes from environment variables (optionally a .env file next to this
module). See .env.example. No secrets live in this file.

Run:  python server/app.py   ->   http://localhost:8000
"""

import os
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory
from hdbcli import dbapi

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_dotenv():
    """Minimal .env loader (KEY=VALUE per line) so python-dotenv isn't required."""
    path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            # don't clobber a value already set in the real environment
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

HANA = {
    "address": os.environ.get("HANA_HOST", ""),
    "port": int(os.environ.get("HANA_PORT", "0") or 0),
    "user": os.environ.get("HANA_USER", ""),
    "password": os.environ.get("HANA_PASSWORD", ""),
    "encrypt": os.environ.get("HANA_ENCRYPT", "true").lower() == "true",
}
PORT = int(os.environ.get("PORT", "8000"))

WEBAPP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "webapp"))


def get_conn():
    """Open a fresh HANA connection. Raises RuntimeError if not configured."""
    if not HANA["address"] or not HANA["port"]:
        raise RuntimeError(
            "HANA connection is not configured. Copy server/.env.example to "
            "server/.env and fill in HANA_HOST / HANA_PORT / HANA_USER / HANA_PASSWORD."
        )
    return dbapi.connect(
        address=HANA["address"],
        port=HANA["port"],
        user=HANA["user"],
        password=HANA["password"],
        encrypt=HANA["encrypt"],
        sslValidateCertificate=False,
    )


# ---------------------------------------------------------------------------
# App + static SAPUI5 serving (same origin as the API, so no CORS needed)
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder=None)


@app.route("/")
def index():
    return send_from_directory(WEBAPP_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(WEBAPP_DIR, path)


# ---------------------------------------------------------------------------
# Finishing (Sanfor) API
# ---------------------------------------------------------------------------

FIN_TABLE = "SAPHANADB.ZFN_FAB_PRD_D"
FIN_MANDT = "900"

# Scanned context echoed from /scan back into /records (all strings from HANA).
_FIN_SCAN_FIELDS = (
    "BATCH_NO", "ARTICLE", "DYESET", "SALES_ORDER_NO", "LOT_NO",
    "BEAM_NO", "LOOM_NO", "LEGACY_NO", "DOFF_BATCHNO", "DOFF_LENGTH",
)


@app.get("/api/finishing/scan")
def api_finishing_scan():
    """Look up doff production context for a scanned DOFF_BATCHNO."""
    doff = (request.args.get("doff") or "").strip()
    if not doff:
        return jsonify({"error": "Missing doff batch number."}), 400

    try:
        conn = get_conn()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT A.BATCH_NO, A.ARTICLE, A.DYESET, A.SALES_ORDER_NO, A.LOT_NO, "
            "A.BEAM_NO, A.LOOM_NO, A.LEGACY_NO, B.DOFF_BATCHNO, B.DOFF_LENGTH "
            "FROM SAPHANADB.ZWV_DOF_D A "
            "INNER JOIN SAPHANADB.ZWV_DOF_DD2 B ON A.DOCID = B.DOCID "
            "WHERE A.MANDT = ? AND B.DOFF_BATCHNO = ?",
            [FIN_MANDT, doff],
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Batch not found for the scanned code."}), 404
        record = {col: ("" if row[i] is None else str(row[i]))
                  for i, col in enumerate(_FIN_SCAN_FIELDS)}
        return jsonify(record)
    except dbapi.Error as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


# Column order for the ZFN_FAB_PRD_D insert. Every other (omitted) column is
# NOT NULL but carries a DB default, so listing only these is safe.
_FIN_COLUMNS = (
    "MANDT, DOCID, DYESET_CD, ARTICLE, LOTNO, LEGACY_NO, OPERATOR, USERIN, USERUP, "
    "PALLATE_NO, BATCHER_NO, FINISH_LENGTH, PROCESS_TYPE, MACHINE_WORKCEN, OPERATION, "
    "FINISH_TYPE, REMARKS, DATEIN, TIMEIN, START_DATE, START_TIME, DATEUP, STOP_DATE, "
    "STOP_TIME, TIMEUP, TIMEMINUTES, DOC_DATE, BATCH_NO, DOFF_BATCH_NO, SALES_ORDER_NO, "
    "LOOM_NO, DOFF_LENGTH"
)
_FIN_PLACEHOLDERS = ", ".join(["?"] * 32)

_FIN_REQUIRED_INPUT = ("operator", "palate", "batcher", "finishLength", "workcen", "processType")


def _fin_next_docid(cur):
    """Next numeric DOCID for MANDT 900, zero-padded to 10 (guards non-numeric ids)."""
    cur.execute(
        f"SELECT IFNULL(MAX(TO_BIGINT(DOCID)), 0) + 1 FROM {FIN_TABLE} "
        f"WHERE MANDT = ? AND DOCID <> '' AND DOCID NOT LIKE '%[^0-9]%'",
        [FIN_MANDT],
    )
    return int(cur.fetchone()[0])


def _parse_dt(value):
    """Parse an ISO-8601 timestamp coming from the browser (handles trailing 'Z')."""
    s = str(value).strip().replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def _dec(value):
    """Coerce a scanned length to a number for the DECIMAL DOFF_LENGTH column."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _fin_insert(conn, scan, inp, started, stopped):
    """Insert one finished Sanfor run. Returns the generated DOCID string."""
    seconds = max(0, round((stopped - started).total_seconds()))
    minutes = round(seconds / 60)

    start_date = started.strftime("%Y%m%d")
    start_time = started.strftime("%H%M%S")
    stop_date = stopped.strftime("%Y%m%d")
    stop_time = stopped.strftime("%H%M%S")
    doc_date = datetime.now().strftime("%Y%m%d")
    operator = inp["operator"]

    cur = conn.cursor()
    docid = str(_fin_next_docid(cur)).zfill(10)

    params = [
        FIN_MANDT,                       # MANDT
        docid,                           # DOCID
        scan.get("DYESET", ""),          # DYESET_CD
        scan.get("ARTICLE", ""),         # ARTICLE
        scan.get("LOT_NO", ""),          # LOTNO
        scan.get("BEAM_NO", ""),         # LEGACY_NO
        operator,                        # OPERATOR
        operator,                        # USERIN
        operator,                        # USERUP
        inp["palate"],                   # PALLATE_NO
        inp["batcher"],                  # BATCHER_NO
        int(inp["finishLength"]),        # FINISH_LENGTH
        inp["processType"],              # PROCESS_TYPE (dropdown text)
        inp["workcen"],                  # MACHINE_WORKCEN (dropdown value)
        "SNFR",                          # OPERATION
        "FINISH",                        # FINISH_TYPE
        "ZWFN",                          # REMARKS (the TCODE)
        start_date,                      # DATEIN
        start_time,                      # TIMEIN
        start_date,                      # START_DATE
        start_time,                      # START_TIME
        stop_date,                       # DATEUP
        stop_date,                       # STOP_DATE
        stop_time,                       # STOP_TIME
        str(seconds),                    # TIMEUP (total running seconds)
        minutes,                         # TIMEMINUTES (total running minutes)
        doc_date,                        # DOC_DATE
        scan.get("BATCH_NO", ""),        # BATCH_NO
        scan.get("DOFF_BATCHNO", ""),    # DOFF_BATCH_NO
        scan.get("SALES_ORDER_NO", ""),  # SALES_ORDER_NO
        scan.get("LOOM_NO", ""),         # LOOM_NO
        _dec(scan.get("DOFF_LENGTH")),   # DOFF_LENGTH
    ]
    cur.execute(f"INSERT INTO {FIN_TABLE} ({_FIN_COLUMNS}) VALUES ({_FIN_PLACEHOLDERS})", params)
    conn.commit()
    cur.close()
    return docid


@app.post("/api/finishing/records")
def api_finishing_create():
    data = request.get_json(silent=True) or {}
    scan = data.get("scan") or {}
    inp = data.get("input") or {}

    # --- validation -------------------------------------------------------
    missing = [f for f in _FIN_REQUIRED_INPUT if not str(inp.get(f, "")).strip()]
    if missing:
        return jsonify({"error": "Missing required field(s): " + ", ".join(missing)}), 400
    try:
        int(str(inp["finishLength"]).strip())
    except (TypeError, ValueError):
        return jsonify({"error": "Finish Length must be a whole number."}), 400
    if not str(scan.get("DOFF_BATCHNO", "")).strip():
        return jsonify({"error": "No scanned batch context. Scan a doff QR first."}), 400
    try:
        started = _parse_dt(data.get("startedAt"))
        stopped = _parse_dt(data.get("stoppedAt"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid or missing machine start/stop timestamps."}), 400
    if stopped < started:
        return jsonify({"error": "Machine stop time is before start time."}), 400

    clean = {f: str(inp[f]).strip() for f in _FIN_REQUIRED_INPUT}

    # --- insert (retry once on a concurrent key clash) --------------------
    try:
        conn = get_conn()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    try:
        for attempt in range(2):
            try:
                docid = _fin_insert(conn, scan, clean, started, stopped)
                return jsonify({"docid": docid}), 201
            except dbapi.Error as exc:
                if attempt == 0 and getattr(exc, "errorcode", None) == 301:
                    continue
                return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True)
