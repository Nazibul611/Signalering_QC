# Signalering QC (QGIS plugin)

Sample-based quality control for BGT/BAG signalering deliveries
(Pinpoint / Pinline / Pinpolygon layers). QGIS 3.22 or newer.

## Install
QGIS -> Plugins -> Manage and Install Plugins -> **Install from ZIP** -> select `signalering_qc.zip`.
A toolbar "Signalering QC" and a menu "Plugins -> Signalering QC" appear.

## One-time setup per project
1. Create two **map themes** (Layers panel -> eye icon -> "Add Theme"), e.g. `2025` and `2026`,
   with the matching aerial photos visible. The review panel switches between them with one key.
2. **1. Settings**: pick the pinpoint (and optional pinline / pinpolygon) layers. Leave
   "Create missing QC fields" ticked. Save the project (settings are stored in the project file).

## Workflow
1. **Define review areas (draw / grid)** - the reviewer decides which areas are reviewed. Click the toolbar
   button; a small window opens with these tools:
   * *Draw polygon on the map*: left-click the corners, right-click / double-click / Enter to finish,
     Backspace removes the last corner, Esc cancels.
   * *Draw rectangle on the map*: drag a rectangle.
   * *Pick cells of a regular grid*: choose a cell size (metres, grid lines are drawn on the map); click a
     cell to select it (click again to deselect), or drag a rectangle to select many cells (Ctrl+drag
     deselects). Cells are multiples of the cell size in the CRS of the pin layer, so the grid is the same
     every time.
   * *Pick cells from a polygon layer (grid shape)*: the same, but the cells are the polygons of a layer in
     the project (your own grid shapefile, buurten, work areas).
   * Every drawn area / picked cell is listed with the number of new pins per operator that it contains.
     "Merge all picked cells into ONE area" turns a selection of cells into one review area. Nothing is
     written until **Save areas**.
   * Every feature belongs to exactly one area (decided by a point on/in the feature). A pin that is
     already in an earlier area stays there, and an overlapping new area only gets the remaining pins; an
     area without new pins is skipped. All features in an area are reviewed, whichever operator made them.
   * Areas are stored in the `QC_Sample` layer (method `manual-polygon`, `manual-rectangle`, `manual-grid`
     or `manual-layer`). Saving again adds a new round (R2-..., R3-...); the dashboard button "Add extra
     review areas for failing operators" opens the same window.
   * The old random draw is still available as an option in the menu "Plugins -> Signalering QC -> Draw
     random review areas (optional)", but it no longer has a toolbar button.
   * **QC rounds** (TL-01, TL-02, OL-01 ...): the area window and the review panel both have a *QC round*
     bar (select / Add / Remove). A round belongs to the review level that is active (Team Lead -> TL-xx,
     Operator Lead -> OL-xx). New areas are added to the selected round. **Picking an existing area again for
     another round is a recheck**: in the area window use *Re-select existing areas*, or pick the same grid
     cell / polygon again - the plugin recognises it as the existing area and adds it to the round instead of
     creating a duplicate. Picking it again in the same round does nothing.
2. **Run automatic pre-checks** (optional but recommended) - writes rule codes to `QC_AUTO` and adds a
   `QC_prechecks` layer. The review panel shows the codes for the current feature.
3. **Review panel** - pick the QC round, pick an area, then step through its features:
   * **Review as:** pick the review level at the top of the panel, **Team Lead** or **Operator Lead**.
     Each level stores its own result, error type, remark, date, reviewer and round in separate fields
     (Team Lead: `QC_*`, Operator Lead: `OL_*`), so one level never overwrites the other. The panel also
     shows what the other level decided for the current feature. Statistics, reports, symbology and
     "next unreviewed" all follow the active level. The sampled areas are shared by both levels.
   * Ctrl+1 = Accepted, Ctrl+2 = Doubtful, Ctrl+3...Ctrl+9 = the error types (rejects with that type),
     Ctrl+0 = Not detectable
   * Ctrl+Left / Ctrl+Right = previous / next, Ctrl+Alt+T = switch map theme (2025 <-> 2026)
   * **Multi-select mode** (checkbox in the panel): judge several features at once. Tick it, then
     * "Select on map": drag a rectangle (or click) around the pins; **Shift** adds to the selection,
       **Ctrl** removes from it. Only features of the current area can be selected.
     * or use "All unreviewed" / "All in area" / "Clear selection".
     * Type an optional remark, then click Accepted / Doubtful / an error type (or Ctrl+1...Ctrl+9). The
       result, error type and remark are written to every selected feature in one go.
     * "Undo last judgement" reverts the last single or multi-select judgement (same area only).
     * In multi-select mode the panel does not jump to / zoom on the next feature, so the map stays put.
   * "Add missed mutation": click the map to add a point the operator missed; it is stored in the
     pinpoint layer with status `Missed` and counts as a major error.
4. **Dashboard / report** - per-operator error rate and verdict, error types, HTML and CSV export,
   and a button to draw an extra sample for operators with verdict `Not accepted`.

## QC rounds - how results are stored
* Round list: project settings. Which areas are in which round: field `qc_rounds` of `QC_Sample`
  (e.g. `;TL-01;TL-02;`). Results of every round: table `QC_Results` in the same .gpkg (one row per round and
  pin: status, error type, remark, reviewer, date). The pin fields (`QC_STATUS`, ...) always show the
  **latest** judgement of the level, plus the round code in `QC_RND` / `OL_RND`, so symbology and the
  attribute table keep working. In a recheck round every pin starts as "not reviewed"; the review panel shows
  the results of the earlier rounds ("Earlier rounds: ...") next to it.
* The panel's area list, progress and "Next unreviewed" follow the selected round. The dashboard has a
  *QC round* selector: one round, or "Latest result of every pin" (the old behaviour).
* **Remove** a round deletes its results and the missed mutations added in it; the pins fall back to the
  previous round's result. Areas that were only in that round stay in the sample and can be added to another
  round with *Re-select existing areas*.
* The first round you create takes over the areas and results made before rounds existed. If you work
  without creating a round, `TL-01` (or `OL-01`) is created automatically the first time areas are saved.
* Area ids (`R1-A01`) count draws of areas; QC round codes (`TL-01`) count review passes. They are separate.

## Fields added to the signalering layers (names are configurable)
Team Lead: `QC_STATUS`, `QC_FOUTTYP`, `QC_OPMER`, `QC_DATUM`, `QC_DOOR`, `QC_RONDE`, `QC_RND` (QC round code);
Operator Lead: `OL_STATUS`, `OL_FOUTTYP`, `OL_OPMER`, `OL_DATUM`, `OL_DOOR`, `OL_RONDE`, `OL_RND`;
shared: `QC_AREA`, `QC_AUTO`
(all <= 10 characters, so they also work in shapefiles). To reuse existing fields such as
`QC_TL_1` / `QCTL_OPMER`, change the names under `"fields"` in Settings - they must be text fields.

## Acceptance rule
error rate = (major errors + missed) / (reviewed + missed). Minor errors are reported but not counted.
Default: <= 5 % and at least 20 reviewed features per operator. Change `acceptance` in Settings.

## Pre-check rules (Settings -> JSON -> "rules")
Types: `geometry`, `required`, `domain`, `status_remark`, `duplicate`, `min_area`, `spatial_ref`.
The shipped rules are examples based on the Zeist pin attributes (Status, MutatiType, Subtype,
Opmerking...) - adapt values and field names to your rulebook. `min_area` is disabled by default.
`spatial_ref` rules look up the reference layer by its name in the project (e.g. `hitbag_pnd`).
Set `"enabled": false` to switch a rule off.


## Initial analysis (button "1b. Initial analysis (all pins)")
Runs on **all** pins, before any sampling, and never edits your layers. Load the BGT reference layers
in the project first (names in `analysis.reference_layers`: `hitbgt`, `v_hitbgtoth_polygon`,
`v_hitbgtoth_point`). Output: `analysis_report.html`, `analysis_flags.csv`, and a `QC_analysis` map layer
(flagged pins, coloured by severity). Pinpolygon is not analysed.

Point pins - what lies underneath (code, severity). The class named in the remark is compared with six BGT
fields: bgttype, otype, tmain, tsub, fmain, fsub; the flag text says which fields matched. `main: sub` needs both.
* A01 *Nieuw* but a related object (same BGT class, see `analysis.related`) already lies underneath,
  **unless** a *Verwijderen* pin of the same Objecttype removes it (the pin lies on the same BGT object or
  within `analysis.replace_distance_m`, 3 m). That case is a replacement and is not flagged. Use
  `removal_types` in a `related` entry to accept other Verwijderen types.
  For PND/OBW/GBI/VGO/WGI/SHD any object of the same type underneath is a `warning`. For ground classes
  (WGD/BTD/OTD/OWG/WTD, `compare_class`) the class/function is compared with the remark: "nieuw parkeervlak"
  on a voetpad is fine; it is only flagged when the BGT object already is a parkeervlak (FMAIN/TMAIN/TSUB).
  If the remark names no class the flag is `info`.
* A02 nothing from the BGT at the pin; A03 underlying bronhouder is not `analysis.bronhouder` (G0355).
* A04 *Verwijderen* but no object of that class underneath; for ground classes also when the BGT object
  does not have the class/function named in the remark ("verwijderen parkeervlak").
* A05 *Gewijzigd* whose remark ("Gewijzigd naar ...", "Objecttype wijzigen naar ...") names a class/function
  that the BGT object already has. `Objecttype` is read as the NEW class, so another class underneath is normal.

Line pins - reshaping distance: the line is sampled every 0.25 m and measured to the boundary of the
matching BGT polygon (same class, within 3 m, smallest mean distance wins). Max = worst deviation,
mean = average deviation. Classes follow `analysis.shift`: below 30 cm = B02 (map improvement, not a
signalering); 30-60 cm = Categorie 3; 60-100 cm = Categorie 2; above 100 cm = Categorie 1 (B01 when the operator
chose another one; two classes apart is a `warning`, one class apart `info`). B03 = no matching BGT object,
B04 = shift over 5 m (probably wrong match or a real redesign). Pins with Objecttype BAG have no BGT geometry (B00).

If pins already carry a sample area (`QC_AREA`), the report adds "flags per 100 pins" per area.

`dev/run_with_shapely.py` runs the same core outside QGIS (geopandas + shapely) for testing.

## Notes / limitations
* Written for QGIS 3.x (Qt5). Not tested on QGIS 4 / Qt6.
* Re-judging a feature inside the same QC round overwrites its result in that round; other rounds are kept
  (table `QC_Results`). `QC_RONDE` records the area draw (R1, R2, ...).
* With shapefiles, results are written directly to the .dbf (no undo). GeoPackage/PostGIS is safer.
* `sampling.py` has no GUI code and can be used from the QGIS Python console.

## Coordinate for the oblique check
When exactly one pin is selected (map, attribute table, or the review panel) in a signalering layer or the
`QC_analysis` layer, its coordinate is copied to the clipboard and shown in the status bar. Points use the
point itself, lines their midpoint, polygons a point inside. Setting `copy_coordinate` (Settings JSON):
`crs` (default `EPSG:28992`, RD) and `format` (default `{x:.2f},{y:.2f}`). For lat/lon use
`"crs": "EPSG:4326", "format": "{y:.6f},{x:.6f}"`. Menu: "Copy coordinate of selected pin" copies again.

## Changelog
* 0.6.1 - the review level now starts as the default (**Team Lead**) in every QGIS session / project instead of the
  level used last time. The Define review areas window has its own *Review as* selector and follows the level
  chosen in the review panel, so "Add QC round" always offers the code of the level you see (TL-xx / OL-xx).
* 0.6.0 - **QC rounds**: select / add / remove a round (TL-01, OL-01, ...) in the review panel and the area window.
  Results are stored per round in the new table `QC_Results`, so selecting an area again for another round is a
  recheck that keeps the earlier results. New tool *Re-select existing areas*; the same grid cell picked again is
  recognised as a recheck. Dashboard / reports per QC round. New file `qc_rounds.py`, new fields `QC_RND` / `OL_RND`
  and `qc_rounds` in `QC_Sample`.
* 0.5.0 - new toolbar button **2. Define review areas (draw / grid)**: the reviewer draws a polygon or rectangle
  on the map, or picks cells from a regular grid or from a polygon layer ("grid shape"); the random draw moved
  to the menu (optional). New file `area_tool.py`; sample areas get method `manual-*`.
* 0.4.0 - all buttons, statuses, error types, verdicts and reports are now in English (Accepted, Rejected,
  Doubtful, Missed, Not detectable; verdicts Accepted / Not accepted / Too few reviewed). Results written by
  earlier versions (Akkoord, Afgekeurd, ...) are still recognised, and old project settings are switched to the
  English texts automatically. New: review levels **Team Lead** and **Operator Lead** (see the review panel).
  The Status values of the pins themselves (Nieuw / Gewijzigd / Verwijderen) are data and are not changed.
* 0.3.8 - new review button **Not detectable** (Ctrl+0; called "Niet detecteerbaar" in 0.3.8): the feature is not wrong but cannot be detected /
  assessed. It counts as handled (the area can be completed) but is excluded from the QC statistics.
* 0.3.7 - review panel: multi-select mode (rectangle / click selection, batch judgement with a shared remark),
  "Undo last judgement". New file `select_tool.py`.
