sap.ui.define([
	"sap/ui/core/mvc/Controller",
	"sap/ui/model/json/JSONModel",
	"sap/ui/core/library",
	"sap/m/MessageToast",
	"sap/m/MessageBox",
	"sap/m/Dialog",
	"sap/m/Button",
	"sap/ui/core/HTML"
], function (Controller, JSONModel, coreLibrary, MessageToast, MessageBox, Dialog, Button, HTML) {
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
			// Check SAP/HANA availability now and poll periodically.
			this._checkHealth();
			this._healthTimer = setInterval(this._checkHealth.bind(this), 20000);
		},

		onExit: function () {
			this._stopCamera();
			if (this._tickHandle) { clearInterval(this._tickHandle); }
			if (this._healthTimer) { clearInterval(this._healthTimer); }
		},

		/** Fresh empty form/runtime state. */
		_newModel: function () {
			return new JSONModel({
				scanText: "",
				scan: {},
				scanned: false,
				running: false,
				stopped: false,
				sapOnline: true,               // SAP/HANA reachable (updated by _checkHealth)
				inputsEnabled: false,          // scanned && !running && !stopped
				input: {
					operator: "",
					palate: "",
					batcher: "",
					finishLength: "",
					workcen: "",
					finishType: "FINISH",
					chksel: false
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

		// ----- SAP availability ---------------------------------------------

		/** Ping the backend health endpoint and flip the SAP-online banner. */
		_checkHealth: function () {
			var oModel = this.getView().getModel("form");
			fetch("api/health")
				.then(function (res) { return res.json(); })
				.then(function (body) {
					var bOnline = !!(body && body.ok);
					var bWas = oModel.getProperty("/sapOnline");
					oModel.setProperty("/sapOnline", bOnline);
					if (bOnline && bWas === false) {
						MessageToast.show(this._t("sapBackOnline"));
					}
				}.bind(this))
				.catch(function () {
					oModel.setProperty("/sapOnline", false);
				});
		},

		onRetryHealth: function () {
			this._checkHealth();
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
					MessageToast.show(this._t("lblDoffBatchCode") + ": " + (r.body.DOFF_BATCHNO || sDoff));
				}.bind(this))
				.catch(function () {
					oBtn.setBusy(false);
					MessageBox.error(this._t("msgSaveFailed"));
				}.bind(this));
		},

		// ----- Camera QR scan (iPad Safari) ---------------------------------

		/**
		 * Opens the rear camera in a dialog and decodes a QR with jsQR. On a hit
		 * the value is dropped into the scan field and the normal lookup runs.
		 */
		onScanQr: function () {
			// getUserMedia needs a secure context (HTTPS or localhost). On a real
			// iPad reaching this server over the LAN by http:// the camera is
			// blocked by Safari — tell the operator how to fix it.
			if (!window.jsQR) {
				MessageBox.error(this._t("errQrLib"));
				return;
			}
			if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
				MessageBox.warning(this._t("errCameraContext"));
				return;
			}

			if (!this._oScanDialog) {
				this._oScanHtml = new HTML({
					content:
						"<div style='text-align:center'>" +
						"<video id='finQrVideo' autoplay muted playsinline " +
						"style='width:100%;max-height:60vh;background:#000;border-radius:.5rem'></video>" +
						"<canvas id='finQrCanvas' style='display:none'></canvas>" +
						"</div>"
				});
				this._oScanDialog = new Dialog({
					title: this._t("scanQrTitle"),
					contentWidth: "32rem",
					stretchOnPhone: true,
					content: [this._oScanHtml],
					endButton: new Button({
						text: this._t("btnCancel"),
						press: function () { this._oScanDialog.close(); }.bind(this)
					}),
					afterClose: this._stopCamera.bind(this)
				});
				this.getView().addDependent(this._oScanDialog);
			}

			this._oScanDialog.open();
			// Start the stream after the video element is in the DOM.
			setTimeout(this._startCamera.bind(this), 0);
		},

		_startCamera: function () {
			var video = document.getElementById("finQrVideo");
			if (!video) { return; }
			navigator.mediaDevices.getUserMedia({
				audio: false,
				video: { facingMode: { ideal: "environment" } }
			}).then(function (stream) {
				this._mediaStream = stream;
				video.setAttribute("playsinline", "");
				video.srcObject = stream;
				var play = video.play();
				if (play && play.catch) { play.catch(function () {}); }
				this._scanning = true;
				this._decodeTick();
			}.bind(this)).catch(function (err) {
				this._oScanDialog.close();
				var msg = (err && (err.name === "NotAllowedError" || err.name === "SecurityError"))
					? this._t("errCameraDenied") : this._t("errCameraContext");
				MessageBox.warning(msg);
			}.bind(this));
		},

		_decodeTick: function () {
			if (!this._scanning) { return; }
			var video = document.getElementById("finQrVideo");
			var canvas = document.getElementById("finQrCanvas");
			if (!video || !canvas || video.readyState !== video.HAVE_ENOUGH_DATA) {
				this._rafId = window.requestAnimationFrame(this._decodeTick.bind(this));
				return;
			}
			var w = video.videoWidth, h = video.videoHeight;
			canvas.width = w;
			canvas.height = h;
			var ctx = canvas.getContext("2d");
			ctx.drawImage(video, 0, 0, w, h);
			var img = ctx.getImageData(0, 0, w, h);
			var code = window.jsQR(img.data, w, h, { inversionAttempts: "dontInvert" });
			if (code && code.data) {
				this._scanning = false;
				var sValue = String(code.data).trim();
				this.getView().getModel("form").setProperty("/scanText", sValue);
				this._oScanDialog.close();
				this.onScan();
				return;
			}
			this._rafId = window.requestAnimationFrame(this._decodeTick.bind(this));
		},

		_stopCamera: function () {
			this._scanning = false;
			if (this._rafId) {
				window.cancelAnimationFrame(this._rafId);
				this._rafId = null;
			}
			if (this._mediaStream) {
				this._mediaStream.getTracks().forEach(function (t) { t.stop(); });
				this._mediaStream = null;
			}
			var video = document.getElementById("finQrVideo");
			if (video) { video.srcObject = null; }
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
					processType: PROCESS_BY_WORKCEN[d.input.workcen] || "",
					finishType: d.input.finishType || "FINISH",
					chksel: !!d.input.chksel
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
					var sMsg = this._t("msgSaved", [r.body && r.body.docid]);
					if (r.body && r.body.matdoc) {
						sMsg += "\n" + this._t("msgMoved", [r.body.matdoc]);
					}
					var oOpts = { title: this._t("msgSavedTitle"), onClose: this.onReset.bind(this) };
					if (r.body && r.body.warning) {
						MessageBox.warning(sMsg + "\n\n" + r.body.warning, oOpts);
					} else {
						MessageBox.success(sMsg, oOpts);
					}
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
