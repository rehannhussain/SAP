# ZWFN — Finishing (Sanfor) Entry

SAPUI5 shop-floor app for the Finishing department's Sanforizing (SNFR) operation, backed
by a Flask + `hdbcli` API against SAP HANA (`SAPHANADB`).

**Flow:** scan a doff QR (`DOFF_BATCHNO`) → review the read-only batch context → enter
Operator / Palate / Batcher / Finish Length and pick the Sanfor machine → Start / Stop the
machine (live timer) → Save, which inserts a run into `ZFN_FAB_PRD_D`.

## Quick start

```bash
pip install -r server/requirements.txt
cp server/.env.example server/.env     # fill in HANA_HOST / PORT / USER / PASSWORD
python server/app.py                   # http://localhost:8000
```

## Layout

```
server/
  app.py            Flask app: serves webapp at / + /api/finishing/{scan,records}
  requirements.txt  flask + hdbcli
  .env.example      config template (.env is gitignored)
webapp/             SAPUI5 app (namespace finishing.sanfor)
  index.html  Component.js  manifest.json
  view/  controller/  i18n/  img/
```

## Notes

- Replace `webapp/img/logo.svg` with your licensed SAP/company logo.
- Saving requires the HANA user to hold INSERT on `SAPHANADB.ZFN_FAB_PRD_D`
  (`GRANT INSERT ON SAPHANADB.ZFN_FAB_PRD_D TO <user>;`).

See [CLAUDE.md](CLAUDE.md) for architecture and gotchas.
