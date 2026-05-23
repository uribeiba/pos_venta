// static/booking/layout-catalog.js
// Catálogo visual de plantillas (reemplaza el select por cards)

(function() {
  function initCatalog() {
      const select = document.getElementById("id_layout_template");
      if (!select) return;

      // Eliminar contenedor previo si existe (evita duplicados)
      const oldContainer = document.getElementById("layout-catalog");
      if (oldContainer) oldContainer.remove();

      // Crear nuevo contenedor
      const container = document.createElement("div");
      container.id = "layout-catalog";
      container.className = "layout-grid";
      select.parentNode.insertBefore(container, select.nextSibling);

      // Filtrar opciones vacías (la primera suele ser "---------")
      const options = Array.from(select.options).filter(opt => opt.value && opt.value !== "");

      options.forEach(opt => {
          // Leer atributos data (con fallbacks seguros)
          let layoutLower = [];
          try {
              const raw = opt.dataset.layoutLower;
              if (raw) layoutLower = JSON.parse(raw);
          } catch(e) { console.warn("Error parseando layoutLower", e); }

          const cols = parseInt(opt.dataset.cols) || 4;
          const floors = parseInt(opt.dataset.floors) || 1;
          // Solo mostramos preview del primer piso (suficiente para identificación)
          const previewCells = layoutLower.slice(0, 40); // limitar a 40 celdas

          // Crear tarjeta
          const card = document.createElement("div");
          card.className = "layout-card";

          // Título
          const title = document.createElement("div");
          title.className = "layout-title";
          title.innerText = opt.text || "Plantilla sin nombre";
          card.appendChild(title);

          // Previsualización (grid)
          const preview = document.createElement("div");
          preview.className = "layout-preview";
          preview.style.display = "grid";
          preview.style.gridTemplateColumns = `repeat(${cols}, 20px)`;
          preview.style.gap = "2px";
          preview.style.marginBottom = "8px";

          previewCells.forEach(cellType => {
              const cellDiv = document.createElement("div");
              cellDiv.style.width = "20px";
              cellDiv.style.height = "20px";
              cellDiv.style.borderRadius = "4px";
              cellDiv.style.border = "1px solid #ccc";
              cellDiv.style.backgroundColor = getColor(cellType);
              preview.appendChild(cellDiv);
          });
          card.appendChild(preview);

          // Información adicional
          const info = document.createElement("div");
          info.className = "layout-info";
          info.style.fontSize = "12px";
          info.style.color = "#555";
          info.innerText = `${floors} piso(s) · ${Math.ceil(previewCells.length / cols)} filas × ${cols} col`;
          card.appendChild(info);

          // Botón seleccionar
          const btn = document.createElement("button");
          btn.className = "layout-btn";
          btn.innerText = "Usar esta plantilla";
          btn.style.marginTop = "8px";
          btn.style.padding = "4px 12px";
          btn.style.borderRadius = "20px";
          btn.style.border = "none";
          btn.style.backgroundColor = "#3b82f6";
          btn.style.color = "white";
          btn.style.cursor = "pointer";
          btn.onclick = (e) => {
              e.stopPropagation();
              select.value = opt.value;
              select.dispatchEvent(new Event("change", { bubbles: true }));
              // Resaltar tarjeta seleccionada
              document.querySelectorAll(".layout-card").forEach(c => {
                  c.style.border = "1px solid #e2e8f0";
                  c.style.boxShadow = "none";
              });
              card.style.border = "2px solid #22c55e";
              card.style.boxShadow = "0 4px 12px rgba(34,197,94,0.2)";
          };
          card.appendChild(btn);

          // También permitir clic en toda la tarjeta
          card.style.cursor = "pointer";
          card.addEventListener("click", (e) => {
              if (e.target !== btn) btn.click();
          });

          container.appendChild(card);
      });

      // Si hay un valor ya seleccionado, resaltar su tarjeta
      const selectedVal = select.value;
      if (selectedVal) {
          const selectedIndex = select.selectedIndex;
          if (selectedIndex > 0) {
              const cards = document.querySelectorAll(".layout-card");
              const optIndex = selectedIndex - 1; // porque la primera opción vacía está fuera
              if (cards[optIndex]) {
                  cards[optIndex].style.border = "2px solid #22c55e";
                  cards[optIndex].style.boxShadow = "0 4px 12px rgba(34,197,94,0.2)";
              }
          }
      }
  }

  function getColor(type) {
      switch (type) {
          case "L": return "#22c55e";  // asiento
          case "P": return "transparent";
          case "X": return "#e5e7eb";  // bloqueo
          case "E": return "#f59e0b";  // escalera
          case "D": return "#3b82f6";  // puerta
          case "B": return "#ec4899";  // baño
          default:  return "#f0f0f0";
      }
  }

  // Esperar a que el DOM esté listo
  if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", initCatalog);
  } else {
      initCatalog();
  }
})();