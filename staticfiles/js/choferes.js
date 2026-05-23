document.addEventListener('DOMContentLoaded', function() {
    // Previsualización de foto
    const photoInput = document.getElementById('id_photo');
    const photoPreview = document.getElementById('photoPreview');
    if (photoInput) {
        photoInput.addEventListener('change', function(e) {
            if (e.target.files && e.target.files[0]) {
                const reader = new FileReader();
                reader.onload = function(ev) {
                    photoPreview.src = ev.target.result;
                }
                reader.readAsDataURL(e.target.files[0]);
            }
        });
    }

    // Confirmación de eliminación
    const deleteButtons = document.querySelectorAll('.delete-driver');
    const deleteModal = new bootstrap.Modal(document.getElementById('deleteModal'));
    const deleteDriverNameSpan = document.getElementById('deleteDriverName');
    const deleteForm = document.getElementById('deleteForm');

    deleteButtons.forEach(btn => {
        btn.addEventListener('click', function() {
            const driverId = this.getAttribute('data-id');
            const driverName = this.getAttribute('data-name');
            deleteDriverNameSpan.textContent = driverName;
            deleteForm.action = deleteForm.action.replace('/0', `/${driverId}`);
            deleteModal.show();
        });
    });

    // Enfocar campo nombre
    const fullNameInput = document.getElementById('id_full_name');
    if (fullNameInput) fullNameInput.focus();
});