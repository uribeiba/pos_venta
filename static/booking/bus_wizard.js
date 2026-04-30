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