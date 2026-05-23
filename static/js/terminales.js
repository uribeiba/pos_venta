document.addEventListener('DOMContentLoaded', function() {
    // Confirmación de eliminación
    const deleteButtons = document.querySelectorAll('.delete-terminal');
    const deleteModal = new bootstrap.Modal(document.getElementById('deleteModal'));
    const deleteTerminalNameSpan = document.getElementById('deleteTerminalName');
    const deleteForm = document.getElementById('deleteForm');

    deleteButtons.forEach(btn => {
        btn.addEventListener('click', function() {
            const terminalId = this.getAttribute('data-id');
            const terminalName = this.getAttribute('data-name');
            deleteTerminalNameSpan.textContent = terminalName;
            deleteForm.action = deleteForm.action.replace('/0', `/${terminalId}`);
            deleteModal.show();
        });
    });

    // Enfocar campo nombre
    const nameInput = document.getElementById('id_name');
    if (nameInput) nameInput.focus();
});