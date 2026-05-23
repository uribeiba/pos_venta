document.addEventListener('DOMContentLoaded', function() {
    // Previsualización de foto (nuevo)
    const photoInput = document.getElementById('id_photo');
    const photoPreview = document.getElementById('photoPreview');
    if (photoInput && photoPreview) {
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

    // Confirmación de eliminación (existente)
    const deleteButtons = document.querySelectorAll('.delete-assistant');
    const deleteModal = new bootstrap.Modal(document.getElementById('deleteModal'));
    const deleteAssistantNameSpan = document.getElementById('deleteAssistantName');
    const deleteForm = document.getElementById('deleteForm');

    deleteButtons.forEach(btn => {
        btn.addEventListener('click', function() {
            const assistantId = this.getAttribute('data-id');
            const assistantName = this.getAttribute('data-name');
            deleteAssistantNameSpan.textContent = assistantName;
            deleteForm.action = deleteForm.action.replace('/0', `/${assistantId}`);
            deleteModal.show();
        });
    });

    // Enfocar campo nombre (existente)
    const fullNameInput = document.getElementById('id_full_name');
    if (fullNameInput) fullNameInput.focus();
});