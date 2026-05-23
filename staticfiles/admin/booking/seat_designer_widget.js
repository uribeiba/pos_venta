// static/admin/booking/seat_designer_widget.js
(function () {
  function init(root) {
    const grid = root.querySelector("[data-grid]");
    const name = root.dataset.name;
    const rows = parseInt(root.dataset.rows || "0", 10);
    const cols = parseInt(root.dataset.cols || "0", 10);

    // Layout del widget (L,P,X,B,E,U)
    const layoutInput = root.querySelector(`input[name="${name}"]`);

    // Números: ahora buscamos el CAMPO REAL del modelo que nos indica la plantilla via data-nums-field.
    // Si por alguna razón no estuviera, caemos a un <input data-nums> de respaldo.
    const numsFieldName = root.dataset.numsField; // e.g. "numbers_lower" | "numbers_upper"
    const numsInput =
      (numsFieldName && root.querySelector(`textarea[name="${numsFieldName}"], input[name="${numsFieldName}"]`)) ||
      root.querySelector("[data-nums]");

    // Sprites (PNG) que llegan como data-*
    const IMG = {
      green: root.dataset.imgGreen,
      red: root.dataset.imgRed,
      grey: root.dataset.imgGrey,
      bath: root.dataset.imgBath,
      stairs: root.dataset.imgStairs,
      door: root.dataset.imgDoor,
    };

    // ---------- Estado ----------
    let TOOL = "L";        // L,P,X,B,E,U,# ( # = toggle mostrar números )
    let data = [];         // layout lineal
    let nums = [];         // números por celda (string)

    function parseMaybe(raw, fallback) {
      if (!raw) return fallback;
      try { return JSON.parse(raw); } catch (_e) {}
      try { return JSON.parse(raw.replace(/'/g, '"')); } catch (_e) {}
      return fallback;
    }

    // Cargar layout y números (tolerante a JSON con comillas simples)
    data = parseMaybe(layoutInput?.value || "", Array(rows * cols).fill("L"));
    if (!Array.isArray(data) || data.length !== rows * cols) {
      data = Array(rows * cols).fill("L");
    }

    nums = parseMaybe(numsInput?.value || "", Array(rows * cols).fill(""));
    if (!Array.isArray(nums) || nums.length !== rows * cols) {
      nums = Array(rows * cols).fill("");
    }

    // Si ya hay números guardados, mostrar la capa al iniciar
    if (nums.some(n => (n || "").trim() !== "")) {
      grid.classList.add("show-nums");
    }

    // ---------- Render ----------
    const cells = [];

    function bgFor(t) {
      switch (t) {
        case "P": return "transparent";
        case "X": return `url(${IMG.grey})`;
        case "B": return `url(${IMG.bath})`;
        case "E": return `url(${IMG.stairs})`;
        case "U": return `url(${IMG.door})`;
        case "L":
        default:  return `url(${IMG.green})`;
      }
    }

    function writeInputs() {
      if (layoutInput) layoutInput.value = JSON.stringify(data);
      if (numsInput)   numsInput.value   = JSON.stringify(nums);
    }

    function draw() {
      grid.style.gridTemplateColumns = `repeat(${cols}, 36px)`;
      grid.innerHTML = "";
      cells.length = 0;

      for (let i = 0; i < rows * cols; i++) {
        const t = data[i] || "L";
        const cell = document.createElement("div");
        cell.className = "cell " + t;
        cell.style.backgroundImage = bgFor(t);

        const badge = document.createElement("div");
        badge.className = "num";
        badge.textContent = nums[i] || "";
        cell.appendChild(badge);

        // Pintar herramienta
        cell.addEventListener("mousedown", () => {
          if (TOOL === "#") return; // el 123 solo alterna visibilidad; no pinta
          data[i] = TOOL;
          // Si la celda no es asiento, limpiar número
          if (["P", "X", "B", "E", "U"].includes(data[i])) nums[i] = "";
          cell.className = "cell " + data[i];
          cell.style.backgroundImage = bgFor(data[i]);
          badge.textContent = nums[i] || "";
          writeInputs();
        });

        // Doble click => editar número
        cell.addEventListener("dblclick", () => {
          if (["P", "X", "B", "E", "U"].includes(data[i])) return;
          const val = window.prompt(
            "Número de asiento (solo dígitos). Deja vacío para quitar número:",
            nums[i] || ""
          );
          nums[i] = (val || "").trim();
          badge.textContent = nums[i] || "";
          if (nums[i]) grid.classList.add("show-nums"); // asegura visibilidad si escribió algo
          writeInputs();
        });

        grid.appendChild(cell);
        cells.push(cell);
      }

      writeInputs();
    }

    // ---------- Toolbar ----------
    // Herramientas: L, P, X, B, E, U y el botón 123 (toggle-nums) como acción
    root.querySelectorAll(".tool").forEach((btn) => {
      btn.addEventListener("click", () => {
        root.querySelectorAll(".tool").forEach((x) => x.classList.remove("active"));
        btn.classList.add("active");
        TOOL = btn.dataset.t;
      });
    });
    // Activa la primera herramienta por defecto
    root.querySelector(".tool")?.click();

    // Botones de acción
    root.querySelectorAll(".btn[data-action]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const a = btn.dataset.action;

        if (a === "auto-pasillo") {
          const mid = Math.floor(cols / 2);
          for (let r = 0; r < rows; r++) {
            const i = r * cols + mid;
            data[i] = "P";
            nums[i] = "";
          }
          draw();
          return;
        }

        if (a === "limpiar") {
          data = Array(rows * cols).fill("L");
          nums = Array(rows * cols).fill("");
          draw();
          return;
        }

        if (a === "copiar") {
          window.__SEAT_CLIPBOARD__ = JSON.stringify({ data, nums });
          btn.textContent = "Copiado ✓";
          setTimeout(() => (btn.textContent = "Copiar"), 900);
          return;
        }

        if (a === "pegar") {
          try {
            const clip = JSON.parse(window.__SEAT_CLIPBOARD__ || "");
            if (clip && Array.isArray(clip.data) && clip.data.length === rows * cols) {
              data = clip.data.slice();
              nums = (Array.isArray(clip.nums) && clip.nums.length === rows * cols)
                ? clip.nums.slice()
                : Array(rows * cols).fill("");
              draw();
            }
          } catch (_e) {}
          return;
        }

        // 123 => mostrar/ocultar los números
        if (a === "toggle-nums") {
          grid.classList.toggle("show-nums");
          return;
        }
      });
    });

    // Dibuja y asegura que los inputs viajen en el submit
    draw();
    root.closest("form")?.addEventListener("submit", writeInputs);
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".seat-widget").forEach(init);
  });
})();
