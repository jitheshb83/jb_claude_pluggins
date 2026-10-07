# Report rendering

How the HTML is built: the payment-method split, the interaction layer,
and the chart rules the renderer follows. Read this before changing
anything visual — several of these rules encode bugs already fixed once.

## "How you paid" — payment-method split


`payment_method_breakdown` splits the focus month's expenses by the
instrument the money left from, and renders a share bar, a per-source table
and a **category × source matrix**. It answers "what did I actually put on
the card" — a different question from the category breakdown, not a
restatement of it.

- Card pseudo-institutions are told apart from real accounts by *absence*
  from `dataset["accounts"]`, so no card name is hardcoded.
- The monthly card bill itself is excluded (it is
  `credit_card_settlement`), since the purchases it settles are already
  counted. The method totals therefore sum exactly to the month's
  `true_expense`.
- Also written to `data/<label>-summary.json` under `payment_methods`, so
  the split is available for later analysis without re-reading the report.

## Report rendering & interaction


The HTML is interactive by default — all inline, no CDN and no network
requests, since the page holds real account data and must work offline.

- **Hover/focus tooltips on every mark** (bars, category rows, method
  segments). Values come from a `data-tip` attribute and are inserted with
  `textContent` only — a merchant or category name originates in bank/PDF
  data and is never trusted as markup. Keyboard focus shows exactly what
  hover shows; Escape dismisses.
- **Chart/Table toggle per chart.** Every chart ships a table twin, so a
  tooltip only ever enhances a value and never gates it.
- **Theme toggle** (auto → light → dark), remembered in `localStorage` and
  wrapped in try/catch so a private window still renders.
- **The long category list folds** to the top 10 with a show-all button;
  the table view always holds every row.

Rules the renderer follows, each of which it previously broke:

- **No one-bar charts.** A single-month range renders the figures plus a
  note instead of a one-column chart — a single column implies a
  comparison the data cannot make, which is what made single-month reports
  look broken.
- **Values live on a y-axis, not on every bar cap.** Per-bar labels
  collided (two ~45px numbers over a ~50px pair). The axis rounds up to a
  clean 1/1.5/2/2.5/3/4/5/6/8/10 × 10ⁿ step via `_nice_ceiling`.
- **Text never wears the series colour.** The net and savings figures used
  to be painted red/green; the sign carries direction and the coloured mark
  beside the text carries identity.
- **Theme tokens live on `.viz-root`**, so the page background and text
  colour must be painted there too — setting them on `body` silently failed
  because custom properties only inherit *downward*, which left a white
  page behind dark cards and a near-invisible heading.
- **Modifier class names are namespaced** (`pm-bank`/`pm-card`). A swatch
  marked `class="sw card"` picked up the report's own `.card` component
  padding and border and rendered a 9px swatch as a ~53px block.
- **Bank-vs-card colour is the validated categorical pair** (slot 7 violet
  / slot 3 aqua), deliberately not the income/expense pair — both of those
  series *are* expenses here. In-fill label ink is picked per mode by
  measured contrast (white on violet 8.56:1; ink on aqua 6.99:1), and an
  in-segment label is only drawn above 12% width so it is never clipped.

The palette is the dataviz reference instance; re-validate with that
skill's `validate_palette.js` before changing any series colour.
