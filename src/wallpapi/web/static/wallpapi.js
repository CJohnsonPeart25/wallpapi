/*
  The three things the Batch page needs a browser for, and nothing else.

  Everything the page does to the server is an htmx attribute in the templates. What is left over is
  presentation that htmx does nothing for:

  1. **The preview.** Opening a Wallpaper at full resolution so it can be judged at the size it will
     actually be seen, and marking it from in there without going back to the tile.
  2. **Sizing the grid.** The batch size is a setting, so a fixed column width makes a batch of 4 look
     like a batch of 32 with most of the screen wasted. The columns are chosen to fill one screen.
  3. **The Mix dropdown's closed label.** A <select> shows the selected option's own text when closed,
     so showing "explore — 75/20/5" in the list and "explore" on the control means rewriting that one
     option as it opens and closes. Nothing else gives a native select two labels.

  Vendored, dependency-free and no build step. Delegated listeners throughout, because htmx replaces
  tiles and the whole grid underneath us — a listener bound to a button would go with the button.
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

  /* ---------- the Mix dropdown's two labels ---------- */

  function label(select, attribute) {
    var option = select.options[select.selectedIndex];
    if (option) {
      option.textContent = option.getAttribute(attribute) || option.textContent;
    }
  }

  function collapseMix() {
    var select = document.getElementById("mix-select");
    if (select) {
      label(select, "data-name");
    }
  }

  /* ---------- the preview ---------- */

  var dialog = document.getElementById("preview-dialog");
  var image = document.getElementById("preview-image");
  var source = document.getElementById("preview-source");
  var rail = document.getElementById("preview-verdicts");
  var previewable = dialog && image && typeof dialog.showModal === "function";
  var showing = null; // the id of the wallpaper the preview is open on

  /*
    The tile the preview is open on, looked up afresh every time: marking swaps that element out, so a
    stored reference would be a reference to a tile no longer on the page.
  */
  function openTile() {
    return showing ? document.querySelector('.tile[data-wallpaper-id="' + showing + '"]') : null;
  }

  function syncRail() {
    var tile = openTile();
    if (!rail || !tile) {
      return;
    }
    var drafted = tile.getAttribute("data-draft-verdict") || "";
    var buttons = rail.querySelectorAll(".preview-verdict");
    for (var at = 0; at < buttons.length; at++) {
      var marked = buttons[at].getAttribute("data-verdict") === drafted;
      buttons[at].classList.toggle("marked", marked);
      buttons[at].setAttribute("aria-pressed", marked ? "true" : "false");
    }
  }

  function fullscreen() {
    // Asked of the image, not the dialog: a modal <dialog> is already in the browser's top layer and
    // Chrome refuses to promote it again. Refusal is not an error worth showing anybody either — the
    // preview is already open and already large.
    var promise = document.fullscreenElement
      ? document.exitFullscreen()
      : image.requestFullscreen && image.requestFullscreen();
    if (promise && typeof promise.catch === "function") {
      promise.catch(function () {});
    }
  }

  function openPreview(tile) {
    var thumbnail = tile.querySelector("img[data-full-url]");
    if (!thumbnail) {
      return;
    }
    // Fetched here and not a moment earlier: the markup ships the URL in a data attribute precisely so
    // that a Batch of tiles costs nothing until one of them is asked for.
    image.setAttribute("src", thumbnail.getAttribute("data-full-url"));
    var link = tile.querySelector(".source");
    if (source && link) {
      source.setAttribute("href", link.getAttribute("href"));
    }
    showing = tile.getAttribute("data-wallpaper-id");
    syncRail();
    dialog.showModal();
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!previewable || !target || typeof target.closest !== "function") {
      return;
    }

    if (!dialog.open) {
      // Anywhere on the thumbnail opens the preview. The verdict rail sits on top of its lower edge,
      // so a click meant for a verdict lands on the rail and never reaches here.
      var thumbnail = target.closest(".tile img[data-full-url]");
      if (thumbnail) {
        openPreview(thumbnail.closest(".tile"));
      }
      return;
    }

    if (target.closest("#preview-fullscreen")) {
      fullscreen();
      return;
    }
    if (target.closest("#preview-source")) {
      return; // a link out to Wallhaven, which is not a dismissal
    }
    var verdict = target.closest(".preview-verdict");
    if (verdict) {
      // The preview's rail marks by clicking the tile's own control behind it. Not a second set of
      // htmx attributes posting to /draft: two controls posting the same draft is two places for the
      // marked state to disagree, and the tile swap a mark already triggers is what this needs anyway.
      var tile = openTile();
      var control =
        tile && tile.querySelector('.verdict[data-verdict="' + verdict.getAttribute("data-verdict") + '"]');
      if (control) {
        control.click();
      }
      // Marking is not a dismissal: judging at size and moving on would otherwise be two clicks.
      return;
    }
    // The image, anything else inside the dialog, and the backdrop — which is what a click on the
    // dialog element itself is, the image and the two bars being the only things inside it.
    dialog.close();
  });

  if (previewable) {
    // A fullscreened image has left the dialog's box behind, so the click above no longer covers it.
    image.addEventListener("click", function (event) {
      if (document.fullscreenElement) {
        event.stopPropagation();
        fullscreen();
      }
    });

    // Escape closes a <dialog> natively, so there is nothing to bind for it. This runs however it was
    // dismissed: drop the source so a dismissed preview stops downloading, and leave fullscreen.
    dialog.addEventListener("close", function () {
      image.removeAttribute("src");
      showing = null;
      if (document.fullscreenElement && typeof document.exitFullscreen === "function") {
        var exit = document.exitFullscreen();
        if (exit && typeof exit.catch === "function") {
          exit.catch(function () {});
        }
      }
    });
  }

  /* ---------- when all of it runs ---------- */

  document.addEventListener("DOMContentLoaded", function () {
    fit();
    collapseMix();
  });
  window.addEventListener("resize", fit);
  // Every htmx swap: a bulk mark replaces the grid, a single mark replaces a tile, and a Mix switch
  // replaces the dropdown with a freshly rendered one carrying both its labels again.
  document.addEventListener("htmx:afterSettle", function () {
    fit();
    collapseMix();
    syncRail();
  });

  // Opening the Mix restores the split, closing takes it away again. focusin covers the keyboard,
  // mousedown the pointer, and both are cheap enough to run every time.
  document.addEventListener("focusin", function (event) {
    if (event.target.id === "mix-select") {
      label(event.target, "data-full");
    }
  });
  document.addEventListener("mousedown", function (event) {
    if (event.target.id === "mix-select") {
      label(event.target, "data-full");
    }
  });
  document.addEventListener("focusout", function (event) {
    if (event.target.id === "mix-select") {
      label(event.target, "data-name");
    }
  });
})();
