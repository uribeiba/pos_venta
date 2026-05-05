(function () {

    let currentType = "L";
    let isMouseDown = false;
  
    let lowerEditor = null;
    let upperEditor = null;
  
    function parseJSON(value) {
      try {
        return JSON.parse(value || "[]");
      } catch {
        return [];
      }
    }
  
    function createGrid(containerId, rows, cols, layoutData, numbersData) {
      const container = document.getElementById(containerId);
      container.innerHTML = "";
  
      const grid = document.createElement("div");
      grid.className = "seat-editor-grid";
      grid.style.gridTemplateColumns = `repeat(${cols}, 40px)`;
  
      const cells = [];
  
      for (let r = 0; r < rows; r++) {
        for (let c = 0; c < cols; c++) {
  
          const i = r * cols + c;
  
          const type = layoutData[i] || "L";
          const number = numbersData[i] || "";
  
          const cell = document.createElement("div");
          cell.className = "seat-editor-cell " + type;
          cell.dataset.type = type;
          cell.dataset.index = i;
  
          if (type === "L") {
            cell.textContent = number;
          }
  
          cell.addEventListener("mousedown", function () {
            isMouseDown = true;
            paint(cell);
          });
  
          cell.addEventListener("mouseover", function () {
            if (isMouseDown) paint(cell);
          });
  
          grid.appendChild(cell);
          cells.push(cell);
        }
      }
  
      document.addEventListener("mouseup", () => isMouseDown = false);
  
      container.appendChild(grid);
  
      return {
        cells,
  
        getLayout() {
          return cells.map(c => c.dataset.type);
        },
  
        getNumbers(start = 1) {
          let num = start;
  
          return cells.map(c => {
            if (c.dataset.type === "L") return String(num++);
            return "";
          });
        }
      };
    }
  
    function paint(cell) {
      cell.dataset.type = currentType;
      cell.className = "seat-editor-cell " + currentType;
      cell.textContent = currentType === "L" ? "" : "";
  
      syncAndPreview();
    }
  
    function setupToolbar() {
      document.querySelectorAll(".seat-tool").forEach(btn => {
        btn.addEventListener("click", function () {
          currentType = btn.dataset.type;
  
          document.querySelectorAll(".seat-tool").forEach(b => b.classList.remove("active"));
          btn.classList.add("active");
        });
      });
    }
  
    function syncAndPreview() {
  
      const layoutLower = document.getElementById("id_layout_lower");
      const numbersLower = document.getElementById("id_numbers_lower");
  
      const layoutUpper = document.getElementById("id_layout_upper");
      const numbersUpper = document.getElementById("id_numbers_upper");
  
      if (lowerEditor) {
        layoutLower.value = JSON.stringify(lowerEditor.getLayout());
        numbersLower.value = JSON.stringify(lowerEditor.getNumbers(1));
      }
  
      if (upperEditor) {
        const lowerSeats = lowerEditor.getNumbers(1).filter(x => x).length;
  
        layoutUpper.value = JSON.stringify(upperEditor.getLayout());
        numbersUpper.value = JSON.stringify(
          upperEditor.getNumbers(lowerSeats + 1)
        );
      }
  
      renderPreview();
    }
  
    function renderPreview() {
      const preview = document.getElementById("seat-preview");
      if (!preview) return;
  
      preview.innerHTML = "";
  
      renderDeck(preview, "Piso inferior", lowerEditor);
  
      if (upperEditor) {
        renderDeck(preview, "Piso superior", upperEditor);
      }
    }
  
    function renderDeck(parent, title, editor) {
      const section = document.createElement("div");
      section.className = "preview-deck";
  
      const h = document.createElement("h3");
      h.textContent = title;
  
      const grid = document.createElement("div");
      grid.className = "seat-editor-grid";
  
      const cols = parseInt(document.getElementById("id_cols").value || 4);
      grid.style.gridTemplateColumns = `repeat(${cols}, 40px)`;
  
      const numbers = editor.getNumbers();
  
      editor.cells.forEach((cell, i) => {
        const div = document.createElement("div");
        const type = cell.dataset.type;
  
        div.className = "seat-editor-cell " + type;
  
        if (type === "L") {
          div.textContent = numbers[i] || "";
        }
  
        grid.appendChild(div);
      });
  
      section.appendChild(h);
      section.appendChild(grid);
      parent.appendChild(section);
    }
  
    function initEditor() {
  
      const rowsLower = parseInt(document.getElementById("id_rows_lower").value || 0);
      const rowsUpper = parseInt(document.getElementById("id_rows_upper").value || 0);
      const cols = parseInt(document.getElementById("id_cols").value || 4);
      const floors = parseInt(document.getElementById("id_floors").value || 1);
  
      // 🔥 LEER DATOS EXISTENTES
      const layoutLowerData = parseJSON(document.getElementById("id_layout_lower").value);
      const numbersLowerData = parseJSON(document.getElementById("id_numbers_lower").value);
  
      const layoutUpperData = parseJSON(document.getElementById("id_layout_upper").value);
      const numbersUpperData = parseJSON(document.getElementById("id_numbers_upper").value);
  
      lowerEditor = createGrid("editor-lower", rowsLower, cols, layoutLowerData, numbersLowerData);
  
      if (floors >= 2) {
        upperEditor = createGrid("editor-upper", rowsUpper, cols, layoutUpperData, numbersUpperData);
      } else {
        document.getElementById("editor-upper").innerHTML = "";
        upperEditor = null;
      }
  
      syncAndPreview();
    }
  
    document.addEventListener("DOMContentLoaded", function () {
  
      setupToolbar();
  
      document.getElementById("build-editor").addEventListener("click", initEditor);
  
      document.getElementById("save-layout").addEventListener("click", function () {
        syncAndPreview();
        alert("Layout actualizado correctamente 🚀");
      });
  
      // 🔥 AUTO-CARGA si ya hay datos
      const existing = document.getElementById("id_layout_lower")?.value;
  
      if (existing && existing.length > 5) {
        initEditor();
      }
  
    });
  
  })();

  document.getElementById("save-as-template")?.addEventListener("click", async function () {

    const name = prompt("Nombre de la plantilla:");
    if (!name) return;
  
    const data = {
      name: name,
      floors: document.getElementById("id_floors").value,
      rows_lower: document.getElementById("id_rows_lower").value,
      rows_upper: document.getElementById("id_rows_upper").value,
      cols: document.getElementById("id_cols").value,
  
      layout_lower: JSON.parse(document.getElementById("id_layout_lower").value || "[]"),
      layout_upper: JSON.parse(document.getElementById("id_layout_upper").value || "[]"),
  
      numbers_lower: JSON.parse(document.getElementById("id_numbers_lower").value || "[]"),
      numbers_upper: JSON.parse(document.getElementById("id_numbers_upper").value || "[]"),
  
      prefix_lower: document.getElementById("id_prefix_lower")?.value || "",
      prefix_upper: document.getElementById("id_prefix_upper")?.value || ""
    };
  
    const response = await fetch("/booking/api/save-layout-template/", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCSRFToken()
      },
      body: JSON.stringify(data)
    });
  
    const result = await response.json();
  
    if (result.success) {
      alert("Plantilla guardada 🚀");
    } else {
      alert("Error: " + result.error);
    }
  });
  
  function getCSRFToken() {
    return document.querySelector('[name=csrfmiddlewaretoken]').value;
  }