document.addEventListener('DOMContentLoaded', function() {
    // Confirmación de eliminación
    const deleteBtns = document.querySelectorAll('.delete-bus');
    const deleteModal = new bootstrap.Modal(document.getElementById('deleteModal'));
    const deleteBusNameSpan = document.getElementById('deleteBusName');
    const deleteForm = document.getElementById('deleteForm');
    deleteBtns.forEach(btn => {
        btn.addEventListener('click', function() {
            const busId = this.getAttribute('data-id');
            const busName = this.getAttribute('data-name');
            deleteBusNameSpan.textContent = busName;
            deleteForm.action = deleteForm.action.replace('/0', `/${busId}`);
            deleteModal.show();
        });
    });
    // Enfocar campo placa
    const plateInput = document.getElementById('id_plate');
    if (plateInput) plateInput.focus();
});