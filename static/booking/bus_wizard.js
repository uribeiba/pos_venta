(function () {
    function byId(id) { return document.getElementById(id); }
  
    function toggleWizardVisibility() {
      const tpl = byId("id_template");
      const lw = byId("id_rows_lower_w")?.closest(".form-row");
      const uw = byId("id_rows_upper_w")?.closest(".form-row");
      if (!tpl || !lw || !uw) return;
      const show = tpl.value !== "";
      lw.style.display = show ? "" : "none";
      uw.style.display = show ? "" : "none";
    }
  
    // FUNCIÓN MEJORADA: Auto-completar campos desde plantilla usando data-attributes
    function autoFillFromTemplate() {
        const templateSelect = byId("id_layout_template");
        if (!templateSelect) return;
  
        templateSelect.addEventListener("change", function() {
            const selectedOption = this.options[this.selectedIndex];
            if (!selectedOption || !selectedOption.dataset) return;
  
            // Extraer datos de data-attributes
            const floors = selectedOption.dataset.floors;
            const rowsLower = selectedOption.dataset.rowsLower;
            const rowsUpper = selectedOption.dataset.rowsUpper;
            const cols = selectedOption.dataset.cols;
  
            const floorsField = byId("id_floors");
            const rowsLowerField = byId("id_rows_lower");
            const rowsUpperField = byId("id_rows_upper");
            const colsField = byId("id_cols");
  
            // Solo actualizar si hay valores y los campos existen
            if (floorsField && floors) {
                floorsField.value = floors;
                // Disparar evento change para actualizar visibilidad de piso superior
                floorsField.dispatchEvent(new Event('change', { bubbles: true }));
            }
            if (rowsLowerField && rowsLower) {
                rowsLowerField.value = rowsLower;
            }
            if (rowsUpperField && rowsUpper) {
                rowsUpperField.value = rowsUpper;
            }
            if (colsField && cols) {
                colsField.value = cols;
            }
  
            console.log('Campos auto-completados desde plantilla:', {
                floors, rowsLower, rowsUpper, cols
            });
        });
    }
  
    // Mostrar/ocultar filas superiores cuando cambian los pisos
    function setupFloorsVisibility() {
        const floors = byId("id_floors");
        const upperRow = byId("id_rows_upper")?.closest(".form-row");
        if (floors && upperRow) {
            function refresh() {
                upperRow.style.display = (parseInt(floors.value || "1", 10) >= 2) ? "" : "none";
            }
            refresh();
            floors.addEventListener("change", refresh);
        }
    }
  
    document.addEventListener("DOMContentLoaded", function () {
        // Configurar visibilidad del wizard
        try { 
            toggleWizardVisibility(); 
            const tpl = byId("id_template");
            if (tpl) tpl.addEventListener("change", toggleWizardVisibility);
        } catch (e) {
            console.log('Wizard visibility setup failed:', e);
        }
  
        // Configurar visibilidad de pisos
        setupFloorsVisibility();
  
        // Configurar auto-completado de plantillas
        autoFillFromTemplate();
    });
  })();


  document.addEventListener("DOMContentLoaded", function () {

    const select = document.getElementById("id_layout_template");
    const container = document.getElementById("layout-catalog");
  
    if (!select || !container) return;
  
    const options = Array.from(select.options).filter(o => o.value);
  
    options.forEach(opt => {
  
      const layoutLower = JSON.parse(opt.dataset.layoutLower || "[]");
      const cols = parseInt(opt.dataset.cols || 4);
  
      const card = document.createElement("div");
      card.className = "layout-card";
  
      const title = document.createElement("div");
      title.className = "layout-title";
      title.innerText = opt.text;
  
      const preview = document.createElement("div");
      preview.className = "layout-preview";
      preview.style.gridTemplateColumns = `repeat(${cols}, 16px)`;
  
      layoutLower.forEach(cell => {
        const el = document.createElement("div");
        el.style.width = "16px";
        el.style.height = "16px";
        el.style.borderRadius = "3px";
        el.style.border = "1px solid #ccc";
        el.style.background = getColor(cell);
        preview.appendChild(el);
      });
  
      const btn = document.createElement("button");
      btn.className = "layout-btn";
      btn.innerText = "Usar esta";
  
      btn.addEventListener("click", () => {
        select.value = opt.value;
  
        // 🔥 dispara tu autoFill existente
        select.dispatchEvent(new Event("change"));
  
        highlightSelected(card);
      });
  
      card.appendChild(title);
      card.appendChild(preview);
      card.appendChild(btn);
  
      container.appendChild(card);
    });
  
    function getColor(type) {
      switch (type) {
        case "L": return "#22c55e";
        case "P": return "transparent";
        case "X": return "#e5e7eb";
        case "E": return "#f59e0b";
        case "D": return "#3b82f6";
        case "B": return "#ec4899";
        default: return "#ddd";
      }
    }
  
    function highlightSelected(selectedCard) {
      document.querySelectorAll(".layout-card").forEach(c => {
        c.style.border = "1px solid #ddd";
      });
      selectedCard.style.border = "2px solid #22c55e";
    }
  
  });