# -*- coding: utf-8 -*-
import os
from functools import partial

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import QAction, QApplication, QFileDialog, QMessageBox, QProgressDialog
from qgis.core import Qgis, QgsProject, QgsVectorLayer

from . import analysis_core, analysis_qgis, config, qc_rounds, rules, utils, workflow
from .dialogs import DashboardDialog, SamplingDialog, SettingsDialog

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
MENU = "&Signalering QC"


class SignaleringQC(object):
    def __init__(self, iface):
        self.iface = iface
        self.actions = []
        self.toolbar = None
        self.panel = None
        self.area_panel = None
        self._sel_slots = {}

    # ------------------------------------------------------------------ gui
    def _add(self, text, fn, icon=None, tip=None, toolbar=True):
        path = os.path.join(PLUGIN_DIR, "icons", icon + ".svg") if icon else None
        act = QAction(QIcon(path) if path else QIcon(), text, self.iface.mainWindow())
        act.setToolTip(text)
        act.triggered.connect(lambda _=False: fn())
        if tip:
            act.setStatusTip(tip)
        self.iface.addPluginToMenu(MENU, act)
        if toolbar:
            self.toolbar.addAction(act)
        self.actions.append(act)
        return act

    def initGui(self):
        self._reset_level()
        self.toolbar = self.iface.addToolBar("Signalering QC")
        self.toolbar.setObjectName("SignaleringQCToolbar")
        self._add("1. Settings", self.open_settings, "settings", "Choose signalering layers and QC configuration")
        self._add("1b. Initial analysis (all pins)", self.run_analysis, "analysis",
                  "Check new/removed/changed pins against the BGT and measure reshaping distances")
        self._add("2. Define review areas (draw / grid)", self.define_areas, "area",
                  "Draw the area to be reviewed on the map, or pick it from a grid")
        self._add("Draw random review areas (optional)", self.draw_sample, "draw", toolbar=False)
        self._add("3. Run automatic pre-checks", self.run_prechecks, "prechecks")
        self._add("4. Review panel", self.show_panel, "review")
        self._add("5. Dashboard / report", self.show_dashboard, "dashboard")
        self._add("Copy coordinate of selected pin", self.copy_selected, "copycoord",
                  "Copies the coordinate of the selected pin (also done automatically on selection)")
        self._add("Apply QC symbology", self.apply_symbology, "symbology")
        self._add("Restore original symbology", self.restore_symbology, "restore")
        QgsProject.instance().readProject.connect(self._on_project_read)
        QgsProject.instance().layersAdded.connect(self._watch_layers)
        self._watch_layers(list(QgsProject.instance().mapLayers().values()))

    # ------------------------------------------------------------------ copy coordinate on selection
    def _watch_layers(self, layers):
        for lyr in layers:
            if not isinstance(lyr, QgsVectorLayer) or lyr.id() in self._sel_slots:
                continue
            slot = partial(self._on_selection, lyr)
            lyr.selectionChanged.connect(slot)
            self._sel_slots[lyr.id()] = (lyr, slot)

    def _is_pin_layer(self, layer):
        if layer.name() == "QC_analysis":
            return True
        cfg = config.load_config()
        return any(l is not None and l.id() == layer.id() for l in utils.get_layers(cfg).values())

    def _on_selection(self, layer, *_):
        if layer.selectedFeatureCount() == 1 and self._is_pin_layer(layer):
            self._copy_coordinate(layer)

    def _copy_coordinate(self, layer, notify=True):
        cfg = config.load_config()
        cc = cfg.get("copy_coordinate", {})
        if not cc.get("enabled", True):
            return None
        feat = layer.getFeature(layer.selectedFeatureIds()[0])
        try:
            txt = utils.coordinate_text(layer, feat, cc)
        except Exception as exc:
            self._msg("Could not make the coordinate: %s" % exc, Qgis.Warning)
            return None
        if txt:
            QApplication.clipboard().setText(txt)
            if notify:
                self.iface.statusBarIface().showMessage("Coordinate copied: %s" % txt, 6000)
        return txt

    def copy_selected(self):
        lyr = self.iface.activeLayer()
        if not isinstance(lyr, QgsVectorLayer) or lyr.selectedFeatureCount() != 1:
            self._msg("Select exactly one pin in the active layer first.", Qgis.Warning)
            return
        txt = self._copy_coordinate(lyr, notify=False)
        if txt:
            self._msg("Coordinate copied: %s" % txt, Qgis.Info, 4)

    def unload(self):
        try:
            QgsProject.instance().layersAdded.disconnect(self._watch_layers)
        except Exception:
            pass
        for lyr, slot in self._sel_slots.values():
            try:
                lyr.selectionChanged.disconnect(slot)
            except Exception:
                pass
        self._sel_slots = {}
        try:
            QgsProject.instance().readProject.disconnect(self._on_project_read)
        except Exception:
            pass
        for act in self.actions:
            self.iface.removePluginMenu(MENU, act)
        if self.toolbar is not None:
            del self.toolbar
        if self.area_panel is not None:
            self.area_panel.close()
            self.area_panel = None
        if self.panel is not None:
            self.panel.remove_shortcuts()
            self.iface.removeDockWidget(self.panel)
            self.panel.deleteLater()
            self.panel = None
        self.actions = []

    def _reset_level(self):
        """The review level is remembered per QGIS user, so a level used last time (e.g. Operator Lead)
        would come back by itself: every session / project starts with the default level (Team Lead)."""
        try:
            config.set_active_level(config.load_config(resolved=False).get("default_level", "TL"))
        except Exception:
            pass

    def _on_project_read(self, *_):
        self._reset_level()
        if self.panel is not None:
            self.panel.reload()

    def _msg(self, text, level=None, duration=6):
        self.iface.messageBar().pushMessage("Signalering QC", text,
                                            level=level if level is not None else Qgis.Info, duration=duration)

    def _need_layers(self):
        cfg = config.load_config()
        layers = utils.get_layers(cfg)
        if not any(layers.values()):
            self._msg("Choose the signalering layers first (1. Settings).", Qgis.Warning)
            return None, None
        return cfg, layers

    # ------------------------------------------------------------------ actions
    def open_settings(self):
        cfg = config.load_config(resolved=False)
        dlg = SettingsDialog(cfg, self.iface.mainWindow())
        if not utils.run_dialog(dlg):
            return
        cfg = dlg.result_config()
        config.save_config(cfg)
        layers = utils.get_layers(cfg)
        if dlg.chk_fields.isChecked():
            for lyr in layers.values():
                if lyr is not None and not utils.ensure_fields(lyr, utils.qc_field_specs(cfg)):
                    self._msg("Could not add QC fields to '%s'." % lyr.name(), Qgis.Warning)
        if self.panel is not None:
            self.panel.reload()
        self._msg("Settings saved in the project (save the project to keep them).", Qgis.Success)

    def run_analysis(self):
        cfg, layers = self._need_layers()
        if cfg is None:
            return
        out_dir = QFileDialog.getExistingDirectory(self.iface.mainWindow(), "Folder for the analysis report",
                                                   QgsProject.instance().homePath() or "")
        if not out_dir:
            return
        dlg = QProgressDialog("Preparing...", "", 0, 100, self.iface.mainWindow())
        dlg.setWindowTitle("Initial analysis")
        dlg.setCancelButton(None)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)

        def progress(done, total, text=None):
            if text:
                dlg.setLabelText(text)
            dlg.setValue(int(100.0 * done / max(total, 1)))
            QApplication.processEvents()

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            res = analysis_qgis.run(cfg, layers, out_dir, progress)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            dlg.close()
            QMessageBox.warning(self.iface.mainWindow(), "Initial analysis", str(exc))
            return
        QApplication.restoreOverrideCursor()
        dlg.close()
        names = analysis_core.RULE_NAMES
        txt = "%d pins analysed, %d flagged (warning or higher).\n\n" % (res["summary"]["total"], res["n_flagged"])
        txt += "\n".join("%s %s: %d" % (k, names.get(k, ""), v) for k, v in sorted(res["summary"]["rules"].items()))
        if res["warnings"]:
            txt += "\n\nNotes:\n" + "\n".join(res["warnings"])
        txt += "\n\nReport: %s\nTable: %s\nMap layer: QC_analysis" % (res["html"], res["csv"])
        QMessageBox.information(self.iface.mainWindow(), "Initial analysis", txt)
        QDesktopServices.openUrl(QUrl.fromLocalFile(res["html"]))

    def define_areas(self, hint_operators=None):
        """Open the window where the reviewer draws areas on the map or picks them from a grid."""
        cfg, layers = self._need_layers()
        if cfg is None:
            return
        if self.area_panel is not None:
            self.area_panel.close()
        from .area_tool import AreaPanel
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.area_panel = AreaPanel(self, cfg, layers, hint_operators, self.iface.mainWindow())
        finally:
            QApplication.restoreOverrideCursor()
        self.area_panel.show()
        self.area_panel.raise_()

    def commit_manual_areas(self, entries, infos, crs):
        """Save the areas chosen in the AreaPanel: new areas are written to the sample layer, existing areas
        chosen again are added to the active QC round (a recheck). Returns True when something was saved."""
        cfg, layers = self._need_layers()
        if cfg is None:
            return False
        new_entries = [e for e in entries if e["kind"] != "recheck"]
        rechecks = [e for e in entries if e["kind"] == "recheck"]
        result = workflow.plan_manual(infos, crs, new_entries) if new_entries else \
            {"cells": [], "per_operator": {}, "warnings": [], "crs": crs, "n_cells_total": 0}
        if not result["cells"] and not rechecks:
            QMessageBox.information(self.iface.mainWindow(), "Signalering QC",
                                    "There is nothing to save.\n\n" + "\n".join(result["warnings"] or [
                                        "The chosen areas contain no pins that are not in a sample yet."]))
            return False
        round_label = qc_rounds.active_round(cfg) or "a new QC round (%s-01)" % cfg.get("active_level", "TL")
        lines = ["QC round: %s" % round_label, ""]
        if result["cells"]:
            lines.append("%d new areas with %d pins:" % (len(result["cells"]),
                                                          sum(len(c.members) for c in result["cells"])))
            for op, d in sorted(result["per_operator"].items()):
                lines.append("   %s: %d new pins in %d areas (already sampled %d of %d)" %
                             (op, d["new_features"], d["cells"], d["already"], d["total"]))
        if rechecks:
            lines.append("%d areas to recheck: %s" % (len(rechecks), ", ".join(e["area_id"] for e in rechecks)))
        if result["warnings"]:
            lines += ["", "Notes:"] + result["warnings"]
        lines += ["", "Save?"]
        if QMessageBox.question(self.iface.mainWindow(), "Signalering QC", "\n".join(lines),
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return False
        code, note = qc_rounds.ensure_round(cfg, layers)      # before the areas exist: they must not be 'adopted'
        if code is None:
            QMessageBox.warning(self.iface.mainWindow(), "Signalering QC", note or "Could not create a QC round.")
            return False
        sample = utils.get_sample_layer(cfg)
        info = None
        if result["cells"]:
            path = sample.source().split("|")[0] if sample is not None else ""
            if not path:
                base = QgsProject.instance().absolutePath() or os.path.expanduser("~")
                path, _ = QFileDialog.getSaveFileName(self.iface.mainWindow(), "Sample file for the review areas",
                                                      os.path.join(base, "QC_Sample.gpkg"), "GeoPackage (*.gpkg)")
                if not path:
                    return False
                if not path.lower().endswith(".gpkg"):
                    path += ".gpkg"
            for lyr in layers.values():
                if lyr is not None:
                    utils.ensure_fields(lyr, utils.qc_field_specs(cfg))
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                info = workflow.commit(cfg, layers, result, {"method": "manual", "seed": 0, "qc_round": code}, path)
            except Exception as exc:
                QApplication.restoreOverrideCursor()
                QMessageBox.warning(self.iface.mainWindow(), "Signalering QC", "Commit failed:\n%s" % exc)
                return False
            QApplication.restoreOverrideCursor()
            cfg["layers"]["sample"] = info["sample_layer"].id()
            config.save_config(cfg)
            sample = info["sample_layer"]
            workflow.apply_qc_symbology(self.iface, cfg, layers)
        if rechecks:
            qc_rounds.update_area_rounds(sample, add=code, only_ids=[e["area_id"] for e in rechecks])
        if self.panel is not None:
            self.panel.reload()
        msg = []
        if info:
            msg.append("%d new areas / %d features" % (info["areas"], info["features"]))
        if rechecks:
            msg.append("%d areas added for recheck" % len(rechecks))
        self._msg("QC round %s: %s. %s" % (code, ", ".join(msg), note or "Save the project to keep the settings."),
                  Qgis.Success)
        return True

    def draw_sample(self, preselect=None):
        cfg, layers = self._need_layers()
        if cfg is None:
            return
        dlg = SamplingDialog(cfg, layers, preselect, self.iface.mainWindow())
        if not utils.run_dialog(dlg):
            return
        params = dlg.params()
        if not params["operators"]:
            self._msg("Select at least one operator.", Qgis.Warning)
            return
        if params["method"] == "polygon" and params["polygon_layer"] is None:
            self._msg("Pick a polygon layer for the area type 'Polygon layer'.", Qgis.Warning)
            return
        if not params["path"]:
            self._msg("Choose a .gpkg file to store the sample.", Qgis.Warning)
            return
        if not params["path"].lower().endswith(".gpkg"):
            params["path"] += ".gpkg"
        for lyr in layers.values():
            if lyr is not None:
                utils.ensure_fields(lyr, utils.qc_field_specs(cfg))
        params["qc_round"], _note = qc_rounds.ensure_round(cfg, layers)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = workflow.plan(cfg, layers, params)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self.iface.mainWindow(), "Signalering QC", str(exc))
            return
        QApplication.restoreOverrideCursor()
        if not result["cells"]:
            QMessageBox.information(self.iface.mainWindow(), "Signalering QC",
                                    "Nothing could be drawn.\n\n" + "\n\n".join(result["warnings"] or [
                                        "No reason was reported. Check the operators, the area type and the minimum pins per area."]))
            return
        lines = ["%d areas will be added (seed %d, %d candidate areas in total):" %
                 (len(result["cells"]), params["seed"], result["n_cells_total"]), ""]
        for op, d in sorted(result["per_operator"].items()):
            lines.append("%s: %d areas, %d new features (already sampled %d of %d)" %
                         (op, d["cells"], d["new_features"], d["already"], d["total"]))
        if result["warnings"]:
            lines += ["", "Notes:"] + result["warnings"]
        lines += ["", "Apply this sample?"]
        ans = QMessageBox.question(self.iface.mainWindow(), "Signalering QC", "\n".join(lines),
                                   QMessageBox.Yes | QMessageBox.No)
        if ans != QMessageBox.Yes:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            info = workflow.commit(cfg, layers, result, params, params["path"])
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self.iface.mainWindow(), "Signalering QC", "Commit failed:\n%s" % exc)
            return
        QApplication.restoreOverrideCursor()
        cfg["layers"]["sample"] = info["sample_layer"].id()
        config.save_config(cfg)
        workflow.apply_qc_symbology(self.iface, cfg, layers)
        self._msg("Round %d: %d areas / %d features drawn. Save the project to keep the settings." %
                  (info["round"], info["areas"], info["features"]), Qgis.Success)
        self.show_panel()

    def run_prechecks(self):
        cfg, layers = self._need_layers()
        if cfg is None:
            return
        ans = QMessageBox.question(self.iface.mainWindow(), "Pre-checks",
                                   "Check only the features inside sampled areas?\n\n"
                                   "Yes = sampled areas only, No = all features.",
                                   QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
        if ans == QMessageBox.Cancel:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            eng = rules.RuleEngine(cfg, layers, only_sampled=(ans == QMessageBox.Yes)).run()
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self.iface.mainWindow(), "Pre-checks", str(exc))
            return
        QApplication.restoreOverrideCursor()
        for old in QgsProject.instance().mapLayersByName("QC_prechecks"):
            QgsProject.instance().removeMapLayer(old.id())
        if eng.flags:
            QgsProject.instance().addMapLayer(rules.build_flag_layer(eng.flags, eng.crs))
        names = {r["id"]: r.get("name", "") for r in cfg["rules"]}
        txt = "\n".join("%s %s: %d" % (k, names.get(k, ""), v) for k, v in sorted(eng.counts.items())) \
            or "No findings."
        if eng.warnings:
            txt += "\n\nWarnings:\n" + "\n".join(eng.warnings)
        txt += "\n\nRule codes were written to the '%s' field; a 'QC_prechecks' layer was added." % cfg["fields"]["auto"]
        QMessageBox.information(self.iface.mainWindow(), "Pre-check result", txt)
        if self.panel is not None:
            self.panel.reload()

    def show_panel(self):
        if self.panel is None:
            from .review_panel import ReviewPanel
            self.panel = ReviewPanel(self.iface, self)
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.panel)
        cfg = config.load_config()
        if not any(utils.get_layers(cfg).values()):
            self._msg("Choose the signalering layers first (1. Settings).", Qgis.Warning)
        self.panel.show()
        self.panel.raise_()
        self.panel.reload()

    def show_dashboard(self):
        cfg, layers = self._need_layers()
        if cfg is None:
            return
        utils.run_dialog(DashboardDialog(self, self.iface.mainWindow()))

    def apply_symbology(self):
        cfg, layers = self._need_layers()
        if cfg is not None:
            workflow.apply_qc_symbology(self.iface, cfg, layers)

    def restore_symbology(self):
        cfg, layers = self._need_layers()
        if cfg is not None:
            workflow.restore_symbology(self.iface, layers)
