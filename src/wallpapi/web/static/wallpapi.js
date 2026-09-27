/*
  The fullscreen preview, and nothing else.

  Everything the Batch page does to the server is an htmx attribute in the templates. This file exists for
  the one thing htmx does nothing for: opening a Wallpaper at full resolution so it can be judged the size
  it will actually be seen at.

  Vendored, dependency-free and no build step. Kept short enough to be reviewed by reading it.

  One delegated listener rather than one per tile, because htmx replaces tiles and the whole grid under
  us — a listener bound to a button would go with the button it was bound to.
*/
(function () {
  "use strict";

  var dialog = document.getElementById("preview-dialog");
  var image = document.getElementById("preview-image");
  if (!dialog || !image || typeof dialog.showModal !== "function") {
    return;
  }

  function open(source) {
    // The full-resolution file is fetched here and not a moment earlier: the markup ships the URL in a
    // data attribute precisely so that a Batch of tiles costs nothing until one of them is asked for.
    image.setAttribute("src", source);
    dialog.showModal();
    // Literal fullscreen where the browser allows it, with the modal dialog as the fallback. The request
    // can be refused (no user gesture, an iframe policy, an unsupported browser) and that is not an error
    // worth showing anybody — the dialog is already open and already covers the viewport.
    if (typeof dialog.requestFullscreen === "function") {
      try {
        var request = dialog.requestFullscreen();
        if (request && typeof request.catch === "function") {
          request.catch(function () {});
        }
      } catch (ignored) {
        /* the dialog fallback is already showing */
      }
    }
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || typeof target.closest !== "function") {
      return;
    }
    var trigger = target.closest("[data-full-url]");
    if (trigger) {
      open(trigger.getAttribute("data-full-url"));
      return;
    }
    if (target.closest("#preview-close")) {
      dialog.close();
      return;
    }
    // A click that lands on the dialog element itself is a click on its backdrop: the image and the close
    // button fill the dialog, so anything else is outside them.
    if (target === dialog) {
      dialog.close();
    }
  });

  // Escape closes a <dialog> natively, so there is nothing to bind for it. This runs however it was
  // dismissed: drop the source so a dismissed preview stops downloading, and leave fullscreen behind.
  dialog.addEventListener("close", function () {
    image.removeAttribute("src");
    if (document.fullscreenElement && typeof document.exitFullscreen === "function") {
      var exit = document.exitFullscreen();
      if (exit && typeof exit.catch === "function") {
        exit.catch(function () {});
      }
    }
  });
})();
