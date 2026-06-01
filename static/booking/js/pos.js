// static/booking/js/pos.js
window.POSApp = (function() {
    let tripId, unitPrice, cols, gridLower, gridUpper;
    let selectedSeats = [];
    let currentDeck = 1;

    // Helper: formatear precio
    function formatPrice(price) {
        return '$' + parseInt(price).toLocaleString('es-CL');
    }

    // Obtener cookie CSRF
    function getCookie(name) {
        let value = "; " + document.cookie;
        let parts = value.split("; " + name + "=");
        if (parts.length === 2) return parts.pop().split(";").shift();
    }

    // Actualizar el resumen de venta (acordeón)
    function updateSummary() {
        const container = document.getElementById('selected-seats-container');
        const copyWrapper = document.getElementById('copy-all-btn-wrapper');
        if (!container) return;

        if (selectedSeats.length === 0) {
            container.innerHTML = '<div class="text-muted text-center py-4">Ningún asiento seleccionado</div>';
            if (copyWrapper) copyWrapper.style.display = 'none';
            document.getElementById('total-amount').innerText = formatPrice(0);
            return;
        }
        if (copyWrapper) copyWrapper.style.display = 'block';

        let total = 0;
        let html = '<div class="accordion" id="accordionSeats">';
        selectedSeats.forEach((seat, idx) => {
            total += seat.price;
            const collapsed = idx === 0 ? '' : 'collapsed';
            const show = idx === 0 ? 'show' : '';
            html += `
                <div class="accordion-item">
                    <h2 class="accordion-header">
                        <button class="accordion-button ${collapsed}" type="button" data-bs-toggle="collapse" data-bs-target="#collapse${idx}">
                            <div class="d-flex justify-content-between w-100 me-3">
                                <span><i class="fas fa-chair me-2"></i> P${seat.deck} - Asiento ${seat.number}</span>
                                <span>${formatPrice(seat.price)}</span>
                            </div>
                        </button>
                    </h2>
                    <div id="collapse${idx}" class="accordion-collapse collapse ${show}" data-bs-parent="#accordionSeats">
                        <div class="accordion-body p-3 bg-light" data-seat-idx="${idx}" data-seat-number="${seat.number}" data-seat-deck="${seat.deck}">
                            <div class="row g-2">
                                <div class="col-6"><input type="text" class="form-control form-control-sm passenger-rut" placeholder="RUT"></div>
                                <div class="col-6"><input type="text" class="form-control form-control-sm passenger-name" placeholder="Nombre"></div>
                                <div class="col-6"><input type="email" class="form-control form-control-sm passenger-email" placeholder="Email"></div>
                                <div class="col-6"><input type="tel" class="form-control form-control-sm passenger-phone" placeholder="Teléfono"></div>
                            </div>
                            <div class="text-end mt-2">
                                <button class="btn btn-sm btn-outline-danger remove-seat-btn" data-number="${seat.number}" data-deck="${seat.deck}">Liberar</button>
                            </div>
                        </div>
                    </div>
                </div>
            `;
        });
        html += '</div>';
        container.innerHTML = html;
        document.getElementById('total-amount').innerText = formatPrice(total);

        // Eventos botones liberar
        document.querySelectorAll('.remove-seat-btn').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                const number = btn.dataset.number;
                const deck = parseInt(btn.dataset.deck);
                const seat = selectedSeats.find(s => s.number === number && s.deck === deck);
                if (seat) {
                    await releaseSeat(seat.id, deck, number);
                }
            });
        });
    }

    // Liberar asiento
    async function releaseSeat(seatId, deck, number) {
        try {
            const resp = await fetch(`/api/release/${tripId}/`, {
                method: 'POST',
                headers: { 'X-CSRFToken': getCookie('csrftoken'), 'Content-Type': 'application/x-www-form-urlencoded' },
                body: `seat_id=${seatId}`
            });
            const data = await resp.json();
            if (data.ok) {
                selectedSeats = selectedSeats.filter(s => !(s.number === number && s.deck === deck));
                updateGridStatus(deck, number, 'free', null);
                updateSummary();
            } else {
                alert(data.error || 'Error al liberar');
            }
        } catch(e) { console.error(e); alert('Error de red'); }
    }

    // Retener asiento
    async function holdSeat(seatId, deck, number, service, price) {
        try {
            const resp = await fetch(`/api/hold/${tripId}/`, {
                method: 'POST',
                headers: { 'X-CSRFToken': getCookie('csrftoken'), 'Content-Type': 'application/x-www-form-urlencoded' },
                body: `seat_id=${seatId}`
            });
            const data = await resp.json();
            if (data.ok) {
                selectedSeats.push({ id: seatId, deck, number, service, price });
                updateGridStatus(deck, number, 'my-hold', null);
                updateSummary();
            } else {
                alert(data.error || 'No se pudo retener el asiento');
            }
        } catch(e) { console.error(e); alert('Error de red'); }
    }

    // Actualizar estado visual en la grilla
    function updateGridStatus(deck, number, newStatus, holdUser) {
        const grid = deck === 1 ? gridLower : gridUpper;
        for (let r = 0; r < grid.length; r++) {
            for (let c = 0; c < grid[r].length; c++) {
                const cell = grid[r][c];
                if (cell && cell.number === number) {
                    cell.status = newStatus;
                    if (holdUser) cell.hold_user = holdUser;
                    break;
                }
            }
        }
        refreshDisplay();
    }

    // Refrescar el renderizado del piso actual
    function refreshDisplay() {
        const grid = currentDeck === 1 ? gridLower : gridUpper;
        window.renderDeck(currentDeck, grid, cols);
        attachClickEvents();
    }

    // Asignar eventos click a los asientos
    function attachClickEvents() {
        document.querySelectorAll('.seat-cell').forEach(cell => {
            if (!cell.dataset.seatId) return; // No es asiento
            cell.removeEventListener('click', clickHandler);
            cell.addEventListener('click', clickHandler);
        });
    }

    async function clickHandler(e) {
        const cell = e.currentTarget;
        const deck = parseInt(cell.dataset.deck);
        const number = cell.dataset.number;
        const seatId = cell.dataset.seatId;
        const service = cell.dataset.service;
        const price = parseFloat(cell.dataset.price) || unitPrice;

        if (!seatId) {
            alert('Asiento sin identificación');
            return;
        }

        const alreadySelected = selectedSeats.some(s => s.id == seatId);
        if (alreadySelected) {
            await releaseSeat(seatId, deck, number);
        } else {
            await holdSeat(seatId, deck, number, service, price);
        }
    }

    // Copiar datos del primer pasajero a todos
    function copyMainPassenger() {
        if (selectedSeats.length < 2) return;
        const firstCard = document.querySelector('#accordionSeats .accordion-body');
        if (!firstCard) return;
        const rut = firstCard.querySelector('.passenger-rut').value;
        const name = firstCard.querySelector('.passenger-name').value;
        const email = firstCard.querySelector('.passenger-email').value;
        const phone = firstCard.querySelector('.passenger-phone').value;
        document.querySelectorAll('#accordionSeats .accordion-body').forEach((body, idx) => {
            if (idx === 0) return;
            body.querySelector('.passenger-rut').value = rut;
            body.querySelector('.passenger-name').value = name;
            body.querySelector('.passenger-email').value = email;
            body.querySelector('.passenger-phone').value = phone;
        });
    }

    // Inicializar búsqueda de clientes
    function initCustomerSearch() {
        const searchInput = document.getElementById('customer-search-input');
        const resultsDiv = document.getElementById('customer-results');
        let timeout;

        searchInput.addEventListener('input', function() {
            clearTimeout(timeout);
            const q = this.value.trim();
            if (q.length < 2) {
                resultsDiv.classList.add('d-none');
                return;
            }
            timeout = setTimeout(() => {
                fetch(`/search-customer/?q=${encodeURIComponent(q)}`)
                    .then(res => res.json())
                    .then(data => {
                        if (data.customers && data.customers.length) {
                            resultsDiv.innerHTML = data.customers.map(c => `
                                <div class="customer-item p-2 border-bottom" data-id="${c.id}" data-name="${c.full_name}" data-rut="${c.national_id}">
                                    <strong>${c.full_name}</strong><br><small>${c.national_id}</small>
                                </div>
                            `).join('');
                            resultsDiv.classList.remove('d-none');
                        } else {
                            resultsDiv.innerHTML = '<div class="p-2 text-muted">No encontrado</div>';
                            resultsDiv.classList.remove('d-none');
                        }
                    });
            }, 300);
        });

        document.addEventListener('click', (e) => {
            if (!resultsDiv.contains(e.target) && e.target !== searchInput) {
                resultsDiv.classList.add('d-none');
            }
        });

        resultsDiv.addEventListener('click', (e) => {
            const item = e.target.closest('.customer-item');
            if (item) {
                const name = item.dataset.name;
                const rut = item.dataset.rut;
                const id = item.dataset.id;
                searchInput.value = name;
                document.getElementById('selected-customer-id').value = id;
                // Rellenar primer asiento
                const firstPassengerName = document.querySelector('#accordionSeats .passenger-name');
                const firstPassengerRut = document.querySelector('#accordionSeats .passenger-rut');
                if (firstPassengerName) firstPassengerName.value = name;
                if (firstPassengerRut) firstPassengerRut.value = rut;
                resultsDiv.classList.add('d-none');
            }
        });

        document.getElementById('create-customer-btn').addEventListener('click', async () => {
            const rut = document.getElementById('new-rut').value.trim();
            const name = document.getElementById('new-name').value.trim();
            if (!rut || !name) return alert('RUT y nombre son obligatorios');
            const resp = await fetch('/create-customer/', {
                method: 'POST',
                headers: { 'X-CSRFToken': getCookie('csrftoken'), 'Content-Type': 'application/x-www-form-urlencoded' },
                body: `national_id=${rut}&full_name=${name}&phone=${document.getElementById('new-phone').value}&email=${document.getElementById('new-email').value}`
            });
            const data = await resp.json();
            if (data.success) {
                searchInput.value = data.customer.full_name;
                document.getElementById('selected-customer-id').value = data.customer.id;
                const firstPassengerName = document.querySelector('#accordionSeats .passenger-name');
                if (firstPassengerName) firstPassengerName.value = data.customer.full_name;
                alert('Cliente creado');
                bootstrap.Collapse.getInstance(document.getElementById('newCustomerCollapse')).hide();
            } else {
                alert(data.error);
            }
        });
    }

    // Checkout
    function initCheckout() {
        document.getElementById('confirm-sale').addEventListener('click', async () => {
            if (selectedSeats.length === 0) return alert('Selecciona al menos un asiento');
            const seatsData = [];
            let valid = true;
            document.querySelectorAll('#accordionSeats .accordion-body').forEach(body => {
                const number = body.dataset.seatNumber;
                const deck = parseInt(body.dataset.seatDeck);
                const rut = body.querySelector('.passenger-rut').value.trim();
                const name = body.querySelector('.passenger-name').value.trim();
                if (!name) valid = false;
                seatsData.push({
                    number, deck,
                    passenger_rut: rut || 'SIN RUT',
                    passenger_name: name || 'Pasajero',
                    passenger_email: body.querySelector('.passenger-email').value,
                    passenger_phone: body.querySelector('.passenger-phone').value
                });
            });
            if (!valid) return alert('Completa el nombre de cada pasajero');
            const paymentMethod = document.getElementById('payment-method').value;
            const buyerName = document.getElementById('customer-search-input').value.trim() || 'Cliente General';
            const customerId = document.getElementById('selected-customer-id').value;

            const formData = new FormData();
            formData.append('seats', JSON.stringify(seatsData));
            formData.append('payment_method', paymentMethod);
            if (customerId) formData.append('customer_id', customerId);
            formData.append('buyer_name', buyerName);
            formData.append('csrfmiddlewaretoken', getCookie('csrftoken'));

            const resp = await fetch(`/pos/checkout/${tripId}/`, { method: 'POST', body: formData });
            if (resp.ok) {
                const html = await resp.text();
                document.open();
                document.write(html);
                document.close();
            } else {
                alert('Error al procesar la venta');
            }
        });
    }

    // Cambio de piso
    function initDeckSwitch() {
        document.querySelectorAll('.deck-pills .btn').forEach(btn => {
            btn.addEventListener('click', function() {
                currentDeck = parseInt(this.dataset.deck);
                document.querySelectorAll('.deck-pills .btn').forEach(b => b.classList.remove('active'));
                this.classList.add('active');
                document.getElementById('deck-1-container').style.display = currentDeck === 1 ? 'block' : 'none';
                document.getElementById('deck-2-container').style.display = currentDeck === 2 ? 'block' : 'none';
                refreshDisplay();
            });
        });
    }

    // API pública
    return {
        init: function(tId, uPrice, colCount, gridLow, gridUp) {
            tripId = tId;
            unitPrice = uPrice;
            cols = colCount;
            gridLower = gridLow;
            gridUpper = gridUp;
            currentDeck = 1;

            // Render inicial
            window.renderDeck(1, gridLower, cols);
            window.renderDeck(2, gridUpper, cols);
            attachClickEvents();
            initDeckSwitch();
            initCustomerSearch();
            initCheckout();

            document.getElementById('copy-main-passenger-data')?.addEventListener('click', copyMainPassenger);
            updateSummary();
        }
    };
})();