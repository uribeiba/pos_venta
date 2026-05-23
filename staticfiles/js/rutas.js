document.addEventListener('DOMContentLoaded', function() {
    // --- Formset dinámico para paradas ---
    const stopsTbody = document.getElementById('stops-tbody');
    const addStopBtn = document.getElementById('add-stop');
    const totalForms = document.getElementById('id_stops-TOTAL_FORMS');
    let formCount = parseInt(totalForms.value);

    function updateIndices() {
        // Reasignar name e id de cada campo según el nuevo índice
        const rows = stopsTbody.querySelectorAll('.stop-row');
        rows.forEach((row, idx) => {
            const inputs = row.querySelectorAll('input, select');
            inputs.forEach(input => {
                const name = input.name;
                if (name) {
                    const newName = name.replace(/stops-\d+-/, `stops-${idx}-`);
                    input.name = newName;
                    input.id = newName;
                }
            });
            // También el checkbox DELETE
            const delCheck = row.querySelector('input[name$="-DELETE"]');
            if (delCheck) {
                delCheck.name = `stops-${idx}-DELETE`;
                delCheck.id = `id_stops-${idx}-DELETE`;
            }
        });
        totalForms.value = rows.length;
        formCount = rows.length;
    }

    function addStopRow() {
        const newIndex = formCount;
        const emptyRowHtml = `
        <tr class="stop-row">
            <td><input type="number" name="stops-${newIndex}-order" value="1" class="form-control" style="width:80px" id="id_stops-${newIndex}-order"></td>
            <td><select name="stops-${newIndex}-city" class="form-select" id="id_stops-${newIndex}-city">
                <option value="">---------</option>
                {% for city in cities %}<option value="{{ city.id }}">{{ city.name }}</option>{% endfor %}
            </select></td>
            <td><select name="stops-${newIndex}-terminal" class="form-select" id="id_stops-${newIndex}-terminal">
                <option value="">---------</option>
                {% for terminal in terminals %}<option value="{{ terminal.id }}">{{ terminal.name }}</option>{% endfor %}
            </select></td>
            <td><input type="number" name="stops-${newIndex}-extra_price" step="0.01" value="0" class="form-control" id="id_stops-${newIndex}-extra_price"></td>
            <td class="text-center"><input type="checkbox" name="stops-${newIndex}-is_mandatory" class="form-check-input" id="id_stops-${newIndex}-is_mandatory" checked></td>
            <td><input type="text" name="stops-${newIndex}-notes" maxlength="200" class="form-control" id="id_stops-${newIndex}-notes"></td>
            <td class="text-center"><button type="button" class="btn btn-sm btn-outline-danger remove-stop">🗑️</button></td>
        </tr>`;
        stopsTbody.insertAdjacentHTML('beforeend', emptyRowHtml);
        formCount++;
        totalForms.value = formCount;
        attachRemoveEvents();
    }

    function attachRemoveEvents() {
        document.querySelectorAll('.remove-stop').forEach(btn => {
            btn.removeEventListener('click', removeStopHandler);
            btn.addEventListener('click', removeStopHandler);
        });
    }

    function removeStopHandler(e) {
        const row = e.target.closest('.stop-row');
        if (row) {
            // Si la fila tiene un checkbox DELETE (para instancias existentes), marcarlo
            const delCheck = row.querySelector('input[name$="-DELETE"]');
            if (delCheck) {
                delCheck.value = 'on';
                row.style.display = 'none';
            } else {
                row.remove();
            }
            updateIndices();
        }
    }

    if (addStopBtn) {
        addStopBtn.addEventListener('click', addStopRow);
        attachRemoveEvents();
    }

    // --- Confirmación de eliminación de ruta ---
    const deleteButtons = document.querySelectorAll('.delete-route');
    const deleteModal = new bootstrap.Modal(document.getElementById('deleteModal'));
    const deleteRouteNameSpan = document.getElementById('deleteRouteName');
    const deleteForm = document.getElementById('deleteForm');

    deleteButtons.forEach(btn => {
        btn.addEventListener('click', function() {
            const routeId = this.getAttribute('data-id');
            const routeName = this.getAttribute('data-name');
            deleteRouteNameSpan.textContent = routeName;
            deleteForm.action = deleteForm.action.replace('/0', `/${routeId}`);
            deleteModal.show();
        });
    });
});