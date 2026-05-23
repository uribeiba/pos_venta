document.addEventListener('DOMContentLoaded', function() {
    // Confirmación de eliminación
    const deleteButtons = document.querySelectorAll('.delete-agency');
    const deleteModal = new bootstrap.Modal(document.getElementById('deleteModal'));
    const deleteAgencyNameSpan = document.getElementById('deleteAgencyName');
    const deleteForm = document.getElementById('deleteForm');

    deleteButtons.forEach(btn => {
        btn.addEventListener('click', function() {
            const agencyId = this.getAttribute('data-id');
            const agencyName = this.getAttribute('data-name');
            deleteAgencyNameSpan.textContent = agencyName;
            deleteForm.action = deleteForm.action.replace('/0', `/${agencyId}`);
            deleteModal.show();
        });
    });

    // Enfocar campo nombre
    const nameInput = document.getElementById('id_name');
    if (nameInput) nameInput.focus();
});