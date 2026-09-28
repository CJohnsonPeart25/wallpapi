/*
  Sizing the Batch grid to the viewport, which is the one thing on these pages neither htmx, CSS nor
  Alpine does.

  The batch size is a setting, so a fixed column width makes a batch of 4 look like a batch of 32 with
  most of the screen wasted. The columns are chosen here so the rows fill one screen.

  Everything the page does to the server is an htmx attribute in the templates. The preview, the Mix
  dropdown's two labels and the theme button are small Alpine components in the templates that use
  them. This file is the grid fit and nothing else.

  Vendored, dependency-free and no build step. It looks the grid up afresh on every run, because htmx
  replaces tiles and the whole grid underneath it.
*/
(function () {
  "use strict";

  var FALLBACK_ASPECT = 3 / 2; // only for a grid with no --tile-ratio on it at all
  var BOTTOM_MARGIN = 24; // a little air under the last row, rather than flush to the viewport edge

  /*
    The shape of a tile, read off the grid rather than repeated here.

    The stylesheet declares it as `--tile-ratio` and lays the tiles out with it, so reading it back is
    what keeps the column count worked out below from being about a different grid than the one the
    browser draws. A second copy of the number in this file would be a second thing to keep in step.
  */
  function aspectOf(grid) {
    var declared = window.getComputedStyle(grid).getPropertyValue("--tile-ratio").split("/");
    var shape = parseFloat(declared[0]) / parseFloat(declared[1] || "1");
    return shape > 0 ? shape : FALLBACK_ASPECT;
  }

  /* ---------- sizing the grid ---------- */

  /*
    Every column count whose rows still fit on one screen, largest tiles first.

    Fewer columns means wider tiles, and wider tiles are short enough that another row usually still
    fits — which is why 32 wallpapers come out in fewer columns than the number suggests, and why
    forcing one more row on top of that is a false economy: the tiles have to lose more height than
    they gain width.

    The height left under the last row is left there. It used to go back into the rows as extra tile
    height, which was worth having while thumbnails were cropped to fill their box. They are fitted
    whole now, so a taller box only adds letterbox — the wallpaper is as big as the column width lets
    it be, and nothing below that line changes it.
  */
  function layouts(tiles, width, height, gap, aspect) {
    var fits = [];
    for (var columns = 1; columns <= tiles; columns++) {
      var rows = Math.ceil(tiles / columns);
      var tileWidth = (width - gap * (columns - 1)) / columns;
      if (rows * (tileWidth / aspect) + gap * (rows - 1) <= height) {
        fits.push({ columns: columns, rows: rows, width: tileWidth, empty: columns * rows - tiles });
      }
    }
    return fits;
  }

  function fit() {
    var grid = document.querySelector(".batch");
    if (!grid || !grid.children.length) {
      return;
    }
    var tiles = grid.children.length;
    var styles = window.getComputedStyle(grid);
    var gap = parseFloat(styles.columnGap) || 0;
    var width =
      grid.clientWidth - (parseFloat(styles.paddingLeft) || 0) - (parseFloat(styles.paddingRight) || 0);
    // The grid's own bottom padding comes off too: the rows are laid out inside it, so counting it as
    // usable height is what makes a layout "fit" and then not quite.
    var height =
      window.innerHeight -
      grid.getBoundingClientRect().top -
      (parseFloat(styles.paddingBottom) || 0) -
      BOTTOM_MARGIN;

    var fits = layouts(tiles, width, height, gap, aspectOf(grid));
    // Nothing fits — a big batch in a short window. One tile per column and let the page scroll, which
    // is the honest answer rather than tiles too small to judge.
    var layout = fits.length ? fits[0] : { columns: tiles, rows: 1, empty: 0, width: width / tiles };
    grid.style.gridTemplateColumns = "repeat(" + layout.columns + ", minmax(0, 1fr))";

    // A last row that does not fill its columns is centred, so what is missing is split evenly either
    // side rather than hanging off one end. Nudging its first tile is enough; the rest follow.
    for (var at = 0; at < tiles; at++) {
      grid.children[at].style.gridColumnStart = "";
    }
    var first = grid.children[tiles - (tiles % layout.columns || layout.columns)];
    if (layout.empty > 0 && first) {
      first.style.gridColumnStart = String(Math.floor(layout.empty / 2) + 1);
    }
  }

  /* ---------- when it runs ---------- */

  document.addEventListener("DOMContentLoaded", fit);
  window.addEventListener("resize", fit);
  // Every htmx swap: a bulk mark replaces the grid and a single mark replaces a tile.
  document.addEventListener("htmx:afterSettle", fit);
})();
