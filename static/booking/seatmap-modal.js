// static/booking/seatmap-modal.js
(function () {
  // --- Carga perezosa de Bootstrap 5 (si no existe) ---
  function ensureBootstrap(cb) {
    if (window.bootstrap && typeof window.bootstrap.Modal === "function") {
      cb();
      return;
    }
    // CSS
    if (!document.getElementById("bs5-css")) {
      var link = document.createElement("link");
      link.id = "bs5-css";
      link.rel = "stylesheet";
      link.href =
        "https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css";
      // si el navegador bloquea por SRI, igual cargará; no detenemos la UX
      link.crossOrigin = "anonymous";
      document.head.appendChild(link);
    }
    // JS
    function afterJs() {
      var chk = setInterval(function () {
        if (window.bootstrap && typeof window.bootstrap.Modal === "function") {
          clearInterval(chk);
          cb();
        }
      }, 50);
    }
    if (!document.getElementById("bs5-js")) {
      var s = document.createElement("script");
      s.id = "bs5-js";
      s.src =
        "https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js";
      s.crossOrigin = "anonymous";
      s.onload = afterJs;
      document.head.appendChild(s);
    } else {
      afterJs();
    }
  }

  // --- Inserta el modal Bootstrap una sola vez ---
  function ensureModal() {
    if (document.getElementById("seatmapModal")) return;

    var wrap = document.createElement("div");
    wrap.innerHTML = `
      <div class="modal fade" id="seatmapModal" tabindex="-1" aria-hidden="true">
        <div class="modal-dialog modal-dialog-centered modal-xl" style="max-width:1080px; margin:1.75rem auto;">
          <div class="modal-content">
            <div class="modal-header">
              <h5 class="modal-title">Mapa de asientos</h5>
              <div class="d-flex align-items-center gap-2">
                <button type="button" class="btn btn-outline-secondary btn-sm" id="seatmapFullscreenBtn" title="Pantalla completa">
                  ⛶ Pantalla completa
                </button>
                <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Cerrar"></button>
              </div>
            </div>
            <div class="modal-body p-0" style="height:78vh; overflow:hidden;">
              <iframe id="seatmapFrame" class="seatmap-iframe" src="about:blank" title="Mapa" loading="lazy"></iframe>
            </div>
            <div class="modal-footer">
              <button type="button" class="btn btn-success" data-bs-dismiss="modal">Cerrar</button>
            </div>
          </div>
        </div>
      </div>
    `;
    document.body.appendChild(wrap.firstElementChild);

    // Al cerrar, liberar el iframe
    var modalEl = document.getElementById("seatmapModal");
    modalEl.addEventListener("hidden.bs.modal", function () {
      var f = document.getElementById("seatmapFrame");
      if (f) f.src = "about:blank";
      // si quedó en fullscreen, salimos
      if (document.fullscreenElement) {
        document.exitFullscreen?.();
      }
      // restaurar texto del botón
      var b = document.getElementById("seatmapFullscreenBtn");
      if (b) b.textContent = "⛶ Pantalla completa";
    });

    // Handler pantalla completa
    var fsBtn = document.getElementById("seatmapFullscreenBtn");
    fsBtn.addEventListener("click", function () {
      var dialog = modalEl.querySelector(".modal-dialog"); // contenedor a pantalla completa
      if (!document.fullscreenElement) {
        requestFs(dialog).then(
          () => (fsBtn.textContent = "⤢ Salir pantalla completa"),
          () => {} // si falla, no hacemos nada
        );
      } else {
        exitFs().then(() => (fsBtn.textContent = "⛶ Pantalla completa"));
      }
    });

    // Fallbacks cross-browser Fullscreen API
    function requestFs(el) {
      return (
        el.requestFullscreen?.() ||
        el.webkitRequestFullscreen?.() ||
        el.mozRequestFullScreen?.() ||
        el.msRequestFullscreen?.() ||
        Promise.reject()
      );
    }
    function exitFs() {
      return (
        document.exitFullscreen?.() ||
        document.webkitExitFullscreen?.() ||
        document.mozCancelFullScreen?.() ||
        document.msExitFullscreen?.() ||
        Promise.resolve()
      );
    }

    // Si el usuario sale de fullscreen con ESC o sistema, actualizamos el botón
    document.addEventListener("fullscreenchange", function () {
      var b = document.getElementById("seatmapFullscreenBtn");
      if (!b) return;
      b.textContent = document.fullscreenElement
        ? "⤢ Salir pantalla completa"
        : "⛶ Pantalla completa";
    });
  }

  // --- Abrir el modal con una URL (seatmap) ---
  function openModal(url) {
    ensureBootstrap(function () {
      ensureModal();
      var modalEl = document.getElementById("seatmapModal");
      var iframe = document.getElementById("seatmapFrame");
      iframe.src = url;

      var modal = new window.bootstrap.Modal(modalEl, { backdrop: true });
      modal.show();
    });
  }

  // --- Delegación de click: <a class="js-seatmap" data-seatmap-url="...">Mapa</a> ---
  function attachClickHandler() {
    document.addEventListener(
      "click",
      function (ev) {
        var a = ev.target.closest("a.js-seatmap[data-seatmap-url]");
        if (!a) return;
        ev.preventDefault();
        var url = a.getAttribute("data-seatmap-url") || a.getAttribute("href");
        if (!url) return;
        openModal(url);
      },
      true
    );
  }

  // Init
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", attachClickHandler);
  } else {
    attachClickHandler();
  }
})();
