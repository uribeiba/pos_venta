document.addEventListener('DOMContentLoaded', function() {
    const originInput = document.getElementById('origin-input');
    const destInput = document.getElementById('dest-input');
    const searchForm = document.getElementById('search-form');
    const loadingSpinner = document.getElementById('loadingSpinner');
    const tripsContainer = document.getElementById('tripsContainer');

    function initAutocomplete(inputElement, datalistId) {
        if (!inputElement) return;
        let timeout = null;
        inputElement.addEventListener('input', function() {
            clearTimeout(timeout);
            const query = this.value.trim();
            const datalist = document.getElementById(datalistId);
            if (query.length < 2) {
                datalist.innerHTML = '';
                return;
            }
            timeout = setTimeout(() => {
                fetch(`/api/cities-search/?q=${encodeURIComponent(query)}`)
                    .then(response => response.json())
                    .then(data => {
                        datalist.innerHTML = '';
                        data.forEach(city => {
                            const option = document.createElement('option');
                            option.value = city.name;
                            datalist.appendChild(option);
                        });
                    });
            }, 300);
        });
    }

    initAutocomplete(originInput, 'origin-datalist');
    initAutocomplete(destInput, 'dest-datalist');

    if (searchForm) {
        searchForm.addEventListener('submit', function(e) {
            const btn = this.querySelector('button[type="submit"]');
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Buscando...';
            if (loadingSpinner) loadingSpinner.style.display = 'block';
            if (tripsContainer) tripsContainer.style.opacity = '0.5';
        });
    }

    window.addEventListener('pageshow', function() {
        const btn = document.querySelector('#search-form button[type="submit"]');
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-search"></i> Buscar';
        }
        if (loadingSpinner) loadingSpinner.style.display = 'none';
        if (tripsContainer) tripsContainer.style.opacity = '1';
    });
});