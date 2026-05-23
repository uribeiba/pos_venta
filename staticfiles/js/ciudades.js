document.addEventListener('DOMContentLoaded', function() {
    // Confirmación de eliminación
    const deleteButtons = document.querySelectorAll('.delete-city');
    const deleteModal = new bootstrap.Modal(document.getElementById('deleteModal'));
    const deleteCityNameSpan = document.getElementById('deleteCityName');
    const deleteForm = document.getElementById('deleteForm');

    deleteButtons.forEach(btn => {
        btn.addEventListener('click', function() {
            const cityId = this.getAttribute('data-id');
            const cityName = this.getAttribute('data-name');
            deleteCityNameSpan.textContent = cityName;
            deleteForm.action = deleteForm.action.replace('/0', `/${cityId}`);
            deleteModal.show();
        });
    });

    // Enfocar campo nombre
    const nameInput = document.getElementById('id_name');
    if (nameInput) nameInput.focus();
});