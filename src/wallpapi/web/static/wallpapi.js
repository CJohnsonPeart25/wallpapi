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

  var ASPECT = 16 / 10; // must match the aspect-ratio on .tile img in wallpapi.css
  var BOTTOM_MARGIN = 24; // a little air under the last row, rather than flush to the viewport edge

  /*
    How tall a tile may grow into the height left under the last row.

    The column count is the fewest that fits, so there is always some slack: the next count down
    overshot the screen and this one came in under it. The rows grow into that slack and stop here
    rather than filling the screen exactly — filling exactly makes the aspect drift with every window
    resize, and crops hardest, for a difference only visible on a batch small enough to be one row.
  */
  var TALLEST = 4 / 3;

  /* ---------- sizing the grid ---------- */

  /*
    Every column count whose rows still fit on one screen, largest tiles first.

    Fewer columns means wider tiles, and wider tiles in a 16:10 grid are short enough that another row
    usually still fits — which is why 32 wallpapers come out 7x5 rather than the 8x4 the number
    suggests, and why forcing a sixth row is a false economy. 6x6 tiles would be wider but would have
    to be less than half as tall to fit, which is less tile than 7x5 and a 2.3:1 crop of the wallpaper.
  */
  function layouts(tiles, width, height, gap) {
    var fits = [];
    for (var columns = 1; columns <= tiles; columns++) {
      var rows = Math.ceil(tiles / columns);
      var tileWidth = (width - gap * (columns - 1)) / columns;
      if (rows * (tileWidth / ASPECT) + gap * (rows - 1) <= height) {
        fits.push({ columns: columns, rows: rows, width: tileWidth, empty: columns * rows - tiles });
      }
    }
    return fits;
  }

  function aspectFor(layout, height, gap) {
    var rowHeight = (height - gap * (layout.rows - 1)) / layout.rows;
    if (rowHeight <= 0) {
      return null;
    }
    var stretched = layout.width / rowHeight;
    if (stretched >= ASPECT) {
      return null; // no slack to spend — the rows are already as tall as 16:10 allows
    }
    return Math.max(stretched, TALLEST);
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
    // The grid's own bottom padding comes off too: the rows are laid out inside it and grow into
    // whatever this works out to, so a few tens of pixels of slop would show at the fold.
    var height =
      window.innerHeight -
      grid.getBoundingClientRect().top -
      (parseFloat(styles.paddingBottom) || 0) -
      BOTTOM_MARGIN;

    var fits = layouts(tiles, width, height, gap);
    // Nothing fits — a big batch in a short window. One tile per column and let the page scroll, which
    // is the honest answer rather than tiles too small to judge.
    var layout = fits.length ? fits[0] : { columns: tiles, rows: 1, empty: 0, width: width / tiles };
    grid.style.gridTemplateColumns = "repeat(" + layout.columns + ", minmax(0, 1fr))";

    var aspect = fits.length ? aspectFor(layout, height, gap) : null;
    grid.style.setProperty("--tile-aspect", aspect ? String(aspect) : "");

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
