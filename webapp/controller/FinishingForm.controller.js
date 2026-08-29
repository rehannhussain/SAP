sap.ui.define([
	"sap/ui/core/mvc/Controller",
	"sap/ui/model/json/JSONModel",
	"sap/ui/core/library",
	"sap/m/MessageToast",
	"sap/m/MessageBox"
], function (Controller, JSONModel, coreLibrary, MessageToast, MessageBox) {
	"use strict";

	var ValueState = coreLibrary.ValueState;

	// Work-center code -> process name, so a single ComboBox selection yields
	// both MACHINE_WORKCEN (key) and PROCESS_TYPE (text) for the insert.
	var PROCESS_BY_WORKCEN = {
		"ZMUF_01": "MUZZI SANFOR",
		"ZMSN_01": "MORRISON SANFOR",
		"ZCIS_01": "MONFORT SANFOR",
		"ZRFS_01": "CIBITEX SANFOR"
	};

	return Controller.extend("finishing.sanfor.controller.FinishingForm", {

		onInit: function () {
			this.getView().setModel(this._newModel(), "form");
			this._tickHandle = null;
		},

		/** Fresh empty form/runtime state. */
		_newModel: function () {
			return new JSONModel({
				scanText: "",
				scan: {},
				scanned: false,
				running: false,
				stopped: false,
				inputsEnabled: false,          // scanned && !running && !stopped
				input: {
					operator: "",
					palate: "",
					batcher: "",
					finishLength: "",
					workcen: ""
				},
				startedAt: null,               // ISO string
				stoppedAt: null,               // ISO string
				startedText: "",
				stoppedText: "",
				elapsedText: "00:00:00",
				totalMinutesText: ""
			});
		},

		/** Recompute the single enable flag the input controls bind to. */
		_syncEnabled: function () {
			var m = this.getView().getModel("form");
			var d = m.getData();
			m.setProperty("/inputsEnabled", d.scanned && !d.running && !d.stopped);
		},

		// ----- Scan ----------------------------------------------------------

		onScan: function () {
			var oModel = this.getView().getModel("form");
			var sDoff = (oModel.getProperty("/scanText") || "").trim();
			if (!sDoff) {
				MessageToast.show(this._t("errScanEmpty"));
				return;
			}

			var oBtn = this.byId("btnLoad");
			oBtn.setBusy(true);
			// Relative path — same origin as the Flask API (never hardcode a host).
			fetch("api/finishing/scan?doff=" + encodeURIComponent(sDoff))
				.then(function (res) {
					return res.json().then(function (body) {
						return { ok: res.ok, body: body };
					});
				})
				.then(function (r) {
					oBtn.setBusy(false);
					if (!r.ok) {
						MessageBox.error(r.body && r.body.error ? r.body.error : this._t("errScanNotFound"));
						return;
					}
					oModel.setProperty("/scan", r.body);
					oModel.setProperty("/scanned", true);
					this._syncEnabled();
					MessageToast.show(this._t("lblDoffBatchNo") + ": " + (r.body.DOFF_BATCHNO || sDoff));
				}.bind(this))
				.catch(function () {
					oBtn.setBusy(false);
					MessageBox.error(this._t("msgSaveFailed"));
				}.bind(this));
		},

		// ----- Machine start / stop -----------------------------------------

		onStart: function () {
			if (!this.getView().getModel("form").getProperty("/scanned")) {
				MessageToast.show(this._t("errNoBatch"));
				return;
			}
			if (!this._validateInputs()) {
				MessageToast.show(this._t("errFixInputs"));
				return;
			}

			var oModel = this.getView().getModel("form");
			var oNow = new Date();
			oModel.setProperty("/startedAt", oNow.toISOString());
			oModel.setProperty("/startedText", this._fmtDateTime(oNow));
			oModel.setProperty("/elapsedText", "00:00:00");
			oModel.setProperty("/running", true);
			oModel.setProperty("/stopped", false);
			this._syncEnabled();

			this._startTicker(oNow);
		},

		onStop: function () {
			var oModel = this.getView().getModel("form");
			if (!oModel.getProperty("/running")) {
				return;
			}
			this._stopTicker();

			var oStop = new Date();
			var oStart = new Date(oModel.getProperty("/startedAt"));
			var iSecs = Math.max(0, Math.round((oStop.getTime() - oStart.getTime()) / 1000));
			var iMins = Math.round(iSecs / 60);

			oModel.setProperty("/stoppedAt", oStop.toISOString());
			oModel.setProperty("/stoppedText", this._fmtDateTime(oStop));
			oModel.setProperty("/elapsedText", this._fmtElapsed(iSecs));
			oModel.setProperty("/totalMinutesText", String(iMins));
			oModel.setProperty("/running", false);
			oModel.setProperty("/stopped", true);
			this._syncEnabled();
		},

		_startTicker: function (oStart) {
			var oModel = this.getView().getModel("form");
			this._stopTicker();
			this._tickHandle = setInterval(function () {
				var iSecs = Math.max(0, Math.round((Date.now() - oStart.getTime()) / 1000));
				oModel.setProperty("/elapsedText", this._fmtElapsed(iSecs));
			}.bind(this), 1000);
		},

		_stopTicker: function () {
			if (this._tickHandle) {
				clearInterval(this._tickHandle);
				this._tickHandle = null;
			}
		},

		// ----- Save ----------------------------------------------------------

		onSave: function () {
			var oModel = this.getView().getModel("form");
			var d = oModel.getData();
			if (!d.stopped || !d.startedAt || !d.stoppedAt) {
				MessageToast.show(this._t("errNotStopped"));
				return;
			}

			var oPayload = {
				scan: d.scan,
				input: {
					operator: d.input.operator.trim(),
					palate: d.input.palate.trim(),
					batcher: d.input.batcher.trim(),
					finishLength: String(d.input.finishLength).trim(),
					workcen: d.input.workcen,
					processType: PROCESS_BY_WORKCEN[d.input.workcen] || ""
				},
				startedAt: d.startedAt,
				stoppedAt: d.stoppedAt
			};

			var oBtn = this.byId("btnSave");
			oBtn.setBusy(true);
			fetch("api/finishing/records", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(oPayload)
			})
				.then(function (res) {
					return res.json().then(function (body) {
						return { ok: res.ok, body: body };
					});
				})
				.then(function (r) {
					oBtn.setBusy(false);
					if (!r.ok) {
						MessageBox.error(r.body && r.body.error ? r.body.error : this._t("msgSaveFailed"));
						return;
					}
					MessageBox.success(
						this._t("msgSaved", [r.body && r.body.docid]),
						{ title: this._t("msgSavedTitle"), onClose: this.onReset.bind(this) }
					);
				}.bind(this))
				.catch(function () {
					oBtn.setBusy(false);
					MessageBox.error(this._t("msgSaveFailed"));
				}.bind(this));
		},

		onReset: function () {
			this._stopTicker();
			this.getView().setModel(this._newModel(), "form");
			["inpOperator", "inpPalate", "inpBatcher", "inpFinishLen", "cbProcess"]
				.forEach(function (sId) {
					var oCtrl = this.byId(sId);
					if (oCtrl) {
						oCtrl.setValueState(ValueState.None);
					}
				}.bind(this));
			var oScan = this.byId("inpScan");
			if (oScan) {
				oScan.focus();
			}
		},

		// ----- Helpers -------------------------------------------------------

		_validateInputs: function () {
			var oView = this.getView();
			var d = oView.getModel("form").getData().input;
			var bValid = true;

			var mText = {
				inpOperator: d.operator,
				inpPalate: d.palate,
				inpBatcher: d.batcher,
				cbProcess: d.workcen
			};
			Object.keys(mText).forEach(function (sId) {
				var oCtrl = oView.byId(sId);
				if (!mText[sId] || !String(mText[sId]).trim()) {
					oCtrl.setValueState(ValueState.Error);
					bValid = false;
				} else {
					oCtrl.setValueState(ValueState.None);
				}
			});

			var oLen = oView.byId("inpFinishLen");
			var iLen = parseInt(d.finishLength, 10);
			if (d.finishLength === "" || isNaN(iLen) || String(d.finishLength).indexOf(".") !== -1 || iLen < 0) {
				oLen.setValueState(ValueState.Error);
				bValid = false;
			} else {
				oLen.setValueState(ValueState.None);
			}

			return bValid;
		},

		_pad: function (n) {
			return (n < 10 ? "0" : "") + n;
		},

		_fmtDateTime: function (oDate) {
			return oDate.getFullYear() + "-" + this._pad(oDate.getMonth() + 1) + "-" +
				this._pad(oDate.getDate()) + " " + this._pad(oDate.getHours()) + ":" +
				this._pad(oDate.getMinutes()) + ":" + this._pad(oDate.getSeconds());
		},

		_fmtElapsed: function (iSecs) {
			var h = Math.floor(iSecs / 3600);
			var m = Math.floor((iSecs % 3600) / 60);
			var s = iSecs % 60;
			return this._pad(h) + ":" + this._pad(m) + ":" + this._pad(s);
		},

		_t: function (sKey, aArgs) {
			return this.getOwnerComponent().getModel("i18n")
				.getResourceBundle().getText(sKey, aArgs);
		}
	});
});
