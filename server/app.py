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
import re
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
# HANA connect/communication timeout in ms (fail fast when SAP is down).
CONNECT_TIMEOUT_MS = int(os.environ.get("HANA_TIMEOUT_MS", "5000"))

WEBAPP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "webapp"))


def get_conn():
    """Open a fresh HANA connection. Raises RuntimeError if not configured."""
    if not HANA["address"] or not HANA["port"]:
        raise RuntimeError(
            "HANA connection is not configured. Copy server/.env.example to "
            "server/.env and fill in HANA_HOST / HANA_PORT / HANA_USER / HANA_PASSWORD."
        )
    # Short timeouts so requests fail fast (with a clear message) when SAP/HANA
    # is down, instead of the browser hanging.
    return dbapi.connect(
        address=HANA["address"],
        port=HANA["port"],
        user=HANA["user"],
        password=HANA["password"],
        encrypt=HANA["encrypt"],
        sslValidateCertificate=False,
        connectTimeout=CONNECT_TIMEOUT_MS,
        communicationTimeout=CONNECT_TIMEOUT_MS,
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


@app.get("/api/health")
def api_health():
    """Report whether the SAP HANA server is reachable (SELECT 1 FROM DUMMY)."""
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM DUMMY")
        cur.fetchone()
        cur.close()
        return jsonify({"ok": True})
    except Exception as exc:                     # RuntimeError, dbapi.Error, timeouts…
        return jsonify({"ok": False, "error": str(exc)})
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Finishing (Sanfor) API
# ---------------------------------------------------------------------------

FIN_TABLE = "SAPHANADB.ZFN_FAB_PRD_D"      # detail
FIN_TABLE_M = "SAPHANADB.ZFN_FAB_PRD_M"    # header
FIN_MANDT = "900"

# Scanned context echoed from /scan back into /records (all strings from HANA).
# DD_BATCH_NO is ZWV_DOF_DD2.BATCH_NO (distinct from A.BATCH_NO); it is written to
# ZFN_FAB_PRD_D.DOFF_BATCH_NO on insert.
_FIN_SCAN_FIELDS = (
    "BATCH_NO", "ARTICLE", "DYESET", "SALES_ORDER_NO", "LOT_NO",
    "BEAM_NO", "LOOM_NO", "LEGACY_NO", "DOFF_BATCHNO", "DOFF_LENGTH", "DD_BATCH_NO",
    "DOFF_DOCID", "DOFF_DOCID_DTL",
)


_FIN_SCAN_SELECT = (
    "SELECT A.BATCH_NO, A.ARTICLE, A.DYESET, A.SALES_ORDER_NO, A.LOT_NO, "
    "A.BEAM_NO, A.LOOM_NO, A.LEGACY_NO, B.DOFF_BATCHNO, B.DOFF_LENGTH, B.BATCH_NO, "
    "B.DOCID, B.DOCID_DTL "
    "FROM SAPHANADB.ZWV_DOF_D A "
    "INNER JOIN SAPHANADB.ZWV_DOF_DD2 B ON A.DOCID = B.DOCID "
    "WHERE A.MANDT = ? AND "
)


def _run_scan(conn, condition, value, limit=2):
    """Run the scan SELECT with a variable DOFF_BATCHNO condition. Returns records."""
    cur = conn.cursor()
    cur.execute(_FIN_SCAN_SELECT + condition + f" LIMIT {int(limit)}", [FIN_MANDT, value])
    rows = cur.fetchall()
    cur.close()
    return [
        {col: ("" if r[i] is None else str(r[i])) for i, col in enumerate(_FIN_SCAN_FIELDS)}
        for r in rows
    ]


def _scan_forms(raw):
    """
    Turn a raw scan into (compact, segments).

    Handheld scanners give the plain DOFF_BATCHNO, but some QR labels come
    formatted with separators and a trailing label, e.g. '261042-528-1446-01
    TRIAL'. We drop the trailing label (first whitespace-delimited token), then:
      - compact  = the token with every separator removed ('261042528144601'),
                   used for a direct exact match of a full code;
      - segments = the token split on any run of non-alphanumerics
                   (['261042','528','1446','01']), used to rebuild a
                   lot% + tail pattern when the QR omits the loom code.
    """
    s = (raw or "").strip()
    if not s:
        return "", []
    token = s.split()[0].upper()
    compact = re.sub(r"[^A-Z0-9]", "", token)
    segments = [p for p in re.split(r"[^A-Z0-9]+", token) if p]
    return compact, segments


@app.get("/api/finishing/scan")
def api_finishing_scan():
    """Look up doff production context for a scanned DOFF_BATCHNO."""
    compact, segments = _scan_forms(request.args.get("doff"))
    if not compact:
        return jsonify({"error": "Missing doff batch number."}), 400

    try:
        conn = get_conn()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    try:
        # 1) Exact match on the full code with separators removed (normal scanner
        #    output, incl. a full code that carried a trailing ' TRIAL' label).
        records = _run_scan(conn, "B.DOFF_BATCHNO = ?", compact)

        # 2) Separated 'trial' label: the QR omits the loom code that sits between
        #    the lot and the doff tail, so match lot + wildcard + tail. Only accept
        #    it when it resolves to exactly one batch (never guess between several).
        if not records and len(segments) >= 2:
            pattern = segments[0] + "%" + "".join(segments[1:])
            matches = _run_scan(conn, "B.DOFF_BATCHNO LIKE ?", pattern, limit=2)
            if len(matches) > 1:
                return jsonify({"error": "This QR matches more than one batch. "
                                         "Scan the full code or type it manually."}), 409
            records = matches

        if not records:
            return jsonify({"error": "Batch not found for the scanned code."}), 404
        return jsonify(records[0])
    except dbapi.Error as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


# Column order for the ZFN_FAB_PRD_D insert. Every other (omitted) column is
# NOT NULL but carries a DB default, so listing only these is safe.
_FIN_COLUMNS = (
    "MANDT, DOCID, DOCID_DTL, DYESET_CD, ARTICLE, LOTNO, BEAM_NO, LEGACY_NO, OPERATOR, "
    "USERIN, USERUP, PALLATE_NO, BATCHER_NO, FINISH_LENGTH, PROCESS_TYPE, MACHINE_WORKCEN, "
    "OPERATION, FINISH_TYPE, REMARKS, DATEIN, TIMEIN, START_DATE, START_TIME, DATEUP, "
    "STOP_DATE, STOP_TIME, TIMEUP, TIMEMINUTES, DOC_DATE, BATCH_NO, DOFF_BATCH_NO, "
    "DOFF_DOCID, DOFF_DOCID_DTL, SALES_ORDER_NO, LOOM_NO, DOFF_LENGTH, CHKSEL, SHIFT"
)
_FIN_PLACEHOLDERS = ", ".join(["?"] * 38)


def _hhmmss(seconds):
    """Format an elapsed duration (seconds) as HHMMSS, e.g. 60295 -> '164455'."""
    seconds = max(0, int(seconds))
    return "%02d%02d%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)


def _shift_for(hhmmss):
    """Shift from a HHMMSS time: A 07:00–15:00, B 15:00–23:00, else C."""
    if "070000" <= hhmmss < "150000":
        return "A"
    if "150000" <= hhmmss < "230000":
        return "B"
    return "C"


def _strip_article(article):
    """Drop the first three characters (the 'FF ' prefix), e.g. 'FF HFZ-6514' -> 'HFZ-6514'."""
    return article[3:] if len(article) > 3 else article


# Header (ZFN_FAB_PRD_M) insert columns. Every other column is NOT NULL with a
# DB default, so listing only these is safe.
_FIN_M_COLUMNS = (
    "MANDT, DOCID, DYESET, ARTICLE, LOTNO_SNR, BEAM_NO_SRN, LEGACY_NO_SNR, DOC_DATE, "
    "PROCESS_TYPE, MACHINE_WORKCEN, OPERATION, FINISH_TYPE, AUFNR, OUT_MATNR, OUT_UOM, "
    "DATEIN, TIMEIN, USERIN, DATEUP, TIMEUP, USERUP, TCODE"
)
_FIN_M_PLACEHOLDERS = ", ".join(["?"] * 22)


def _fin_order_lookup(cur, lotno):
    """
    Resolve (AUFNR, OUT_MATNR, OUT_UOM) for a lot, mirroring the ABAP SELECT SINGLE.

    NOTE: on this S/4HANA system MSEG is empty (data moved to MATDOC), so the
    material-document filter reads MATDOC. Returns ('', '', '') when nothing matches.
    """
    lotno = (lotno or "").strip()
    if not lotno:
        return "", "", ""
    cur.execute(
        "SELECT spo.FINISHING_PROD_NO, af.PLNBEZ "
        "FROM SAPHANADB.ZSIZ_PLN_M spm "
        "INNER JOIN SAPHANADB.ZSIZ_PLN_D spd ON spm.DOCID = spd.DOCID "
        "INNER JOIN SAPHANADB.ZSIZPLN_WF_ORD_D spo ON spm.DOCID = spo.DOCID "
        "INNER JOIN SAPHANADB.AFKO af ON spo.WEAVING_PROD_NO = af.AUFNR "
        "WHERE spm.MANDT = ? AND spm.LOT_NO = ? "
        "AND spm.LOT_NO IN (SELECT ABLAD FROM SAPHANADB.MATDOC "
        "WHERE MANDT = ? AND MATNR LIKE '0000000036%' AND BWART = '101' AND AUFNR LIKE '0060%') "
        "LIMIT 1",
        [FIN_MANDT, lotno, FIN_MANDT],
    )
    row = cur.fetchone()
    if not row:
        return "", "", ""
    aufnr = "" if row[0] is None else str(row[0])
    out_matnr = "" if row[1] is None else str(row[1])
    out_uom = ""
    if out_matnr:
        cur.execute(
            "SELECT MEINS FROM SAPHANADB.MARA WHERE MANDT = ? AND MATNR = ?",
            [FIN_MANDT, out_matnr],
        )
        m = cur.fetchone()
        if m and m[0] is not None:
            out_uom = str(m[0])
    return aufnr, out_matnr, out_uom


_FIN_REQUIRED_INPUT = ("operator", "palate", "batcher", "finishLength", "workcen",
                       "processType", "finishType")


def _fin_next_docid(cur):
    """Next DOCID shared by the M header and D detail = MAX over both tables + 1."""
    cur.execute(
        "SELECT GREATEST("
        f"(SELECT IFNULL(MAX(TO_BIGINT(DOCID)),0) FROM {FIN_TABLE_M} "
        "WHERE MANDT=? AND DOCID<>'' AND DOCID NOT LIKE '%[^0-9]%'),"
        f"(SELECT IFNULL(MAX(TO_BIGINT(DOCID)),0) FROM {FIN_TABLE} "
        "WHERE MANDT=? AND DOCID<>'' AND DOCID NOT LIKE '%[^0-9]%')"
        ") + 1 FROM DUMMY",
        [FIN_MANDT, FIN_MANDT],
    )
    return int(cur.fetchone()[0])


def _fin_next_docid_dtl(cur, docid):
    """Next DOCID_DTL line sequence for a given DOCID, zero-padded to 10.

    Each DOCID groups one or more detail lines (0000000001, 0000000002, …). For a
    freshly generated DOCID this is 0000000001.
    """
    cur.execute(
        f"SELECT IFNULL(MAX(TO_INT(DOCID_DTL)), 0) + 1 FROM {FIN_TABLE} "
        f"WHERE MANDT = ? AND DOCID = ? AND DOCID_DTL <> '' "
        f"AND DOCID_DTL NOT LIKE '%[^0-9]%'",
        [FIN_MANDT, docid],
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
    """Insert one finished Sanfor run: header (M) + detail (D) under one shared
    DOCID, in a single transaction. Returns the generated DOCID string."""
    seconds = max(0, round((stopped - started).total_seconds()))
    minutes = round(seconds / 60)
    elapsed = _hhmmss(seconds)

    start_date = started.strftime("%Y%m%d")
    start_time = started.strftime("%H%M%S")
    stop_date = stopped.strftime("%Y%m%d")
    stop_time = stopped.strftime("%H%M%S")
    doc_date = datetime.now().strftime("%Y%m%d")
    operator = inp["operator"]
    article = _strip_article(scan.get("ARTICLE", ""))   # 'FF ' prefix dropped, M and D

    conn.setautocommit(False)                            # header + detail commit together
    cur = conn.cursor()
    docid = str(_fin_next_docid(cur)).zfill(10)
    docid_dtl = str(_fin_next_docid_dtl(cur, docid)).zfill(10)
    aufnr, out_matnr, out_uom = _fin_order_lookup(cur, scan.get("LOT_NO", ""))

    # --- header: ZFN_FAB_PRD_M -------------------------------------------
    m_params = [
        FIN_MANDT,                       # MANDT
        docid,                           # DOCID
        scan.get("DYESET", ""),          # DYESET
        article,                         # ARTICLE (stripped)
        scan.get("LOT_NO", ""),          # LOTNO_SNR
        scan.get("BEAM_NO", ""),         # BEAM_NO_SRN
        scan.get("LEGACY_NO", ""),       # LEGACY_NO_SNR
        doc_date,                        # DOC_DATE
        "FINISH",                        # PROCESS_TYPE (fixed for the header)
        inp["workcen"],                  # MACHINE_WORKCEN
        "SNFR",                          # OPERATION
        inp["finishType"],               # FINISH_TYPE (dropdown)
        aufnr,                           # AUFNR   (order lookup)
        out_matnr,                       # OUT_MATNR
        out_uom,                         # OUT_UOM
        start_date,                      # DATEIN
        start_time,                      # TIMEIN
        operator,                        # USERIN
        stop_date,                       # DATEUP
        elapsed,                         # TIMEUP (elapsed HHMMSS)
        operator,                        # USERUP
        "ZWFN",                          # TCODE
    ]
    cur.execute(f"INSERT INTO {FIN_TABLE_M} ({_FIN_M_COLUMNS}) VALUES ({_FIN_M_PLACEHOLDERS})", m_params)

    # --- detail: ZFN_FAB_PRD_D -------------------------------------------
    d_params = [
        FIN_MANDT,                       # MANDT
        docid,                           # DOCID (shared with the header)
        docid_dtl,                       # DOCID_DTL (line seq for this DOCID)
        scan.get("DYESET", ""),          # DYESET_CD
        article,                         # ARTICLE (first 3 chars dropped)
        scan.get("LOT_NO", ""),          # LOTNO
        scan.get("BEAM_NO", ""),         # BEAM_NO      <- query BEAM_NO
        scan.get("LEGACY_NO", ""),       # LEGACY_NO    <- query LEGACY_NO
        operator,                        # OPERATOR
        operator,                        # USERIN
        operator,                        # USERUP
        inp["palate"],                   # PALLATE_NO
        inp["batcher"],                  # BATCHER_NO
        int(inp["finishLength"]),        # FINISH_LENGTH
        inp["processType"],              # PROCESS_TYPE (dropdown text)
        inp["workcen"],                  # MACHINE_WORKCEN (dropdown value)
        "SNFR",                          # OPERATION
        "FINISH",                        # FINISH_TYPE (fixed for the detail)
        "ZWFN",                          # REMARKS (the TCODE)
        start_date,                      # DATEIN
        start_time,                      # TIMEIN
        start_date,                      # START_DATE
        start_time,                      # START_TIME
        stop_date,                       # DATEUP
        stop_date,                       # STOP_DATE
        stop_time,                       # STOP_TIME
        elapsed,                         # TIMEUP (elapsed HHMMSS)
        minutes,                         # TIMEMINUTES (total running minutes)
        doc_date,                        # DOC_DATE
        scan.get("BATCH_NO", ""),        # BATCH_NO
        scan.get("DD_BATCH_NO", ""),     # DOFF_BATCH_NO  <- ZWV_DOF_DD2.BATCH_NO
        scan.get("DOFF_DOCID", ""),      # DOFF_DOCID     <- ZWV_DOF_DD2.DOCID
        scan.get("DOFF_DOCID_DTL", ""),  # DOFF_DOCID_DTL <- ZWV_DOF_DD2.DOCID_DTL
        scan.get("SALES_ORDER_NO", ""),  # SALES_ORDER_NO
        scan.get("LOOM_NO", ""),         # LOOM_NO
        _dec(scan.get("DOFF_LENGTH")),   # DOFF_LENGTH
        "X" if inp.get("chksel") else "",  # CHKSEL (checkbox)
        _shift_for(start_time),          # SHIFT (from machine start time)
    ]
    cur.execute(f"INSERT INTO {FIN_TABLE} ({_FIN_COLUMNS}) VALUES ({_FIN_PLACEHOLDERS})", d_params)

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
    clean["chksel"] = bool(inp.get("chksel"))

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
                try:
                    conn.rollback()          # discard the partial header+detail
                except Exception:
                    pass
                if attempt == 0 and getattr(exc, "errorcode", None) == 301:
                    continue
                return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


def _ssl_context():
    """
    HTTPS is required for the iPad camera scan (Safari blocks getUserMedia over
    plain http:// on a LAN address). Enable with USE_HTTPS=true:
      - if server/cert.pem + server/key.pem exist, use them (stable, recommended);
      - otherwise fall back to a throwaway 'adhoc' cert (new cert each restart, so
        the iPad must re-accept the warning every time).
    Generate a stable self-signed cert (include your LAN IP) with, e.g.:
      openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
        -keyout server/key.pem -out server/cert.pem -subj "/CN=zwfn-finishing" \
        -addext "subjectAltName=IP:<YOUR_LAN_IP>,IP:127.0.0.1,DNS:localhost"
    """
    if os.environ.get("USE_HTTPS", "false").lower() != "true":
        return None
    here = os.path.dirname(__file__)
    cert = os.path.join(here, "cert.pem")
    key = os.path.join(here, "key.pem")
    if os.path.exists(cert) and os.path.exists(key):
        return (cert, key)
    return "adhoc"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True, ssl_context=_ssl_context())
