document.addEventListener('DOMContentLoaded', function() {
    // --- Previsualización de foto (soporta <img> o <i>) ---
    const photoInput = document.getElementById('id_photo');
    const previewContainer = document.getElementById('photoPreview');

    if (photoInput) {
        photoInput.addEventListener('change', function(e) {
            const file = e.target.files[0];
            if (file && previewContainer) {
                const reader = new FileReader();
                reader.onload = function(ev) {
                    // Si el elemento actual es un <i> (ícono), lo reemplazamos por un <img>
                    if (previewContainer.tagName === 'I') {
                        const newImg = document.createElement('img');
                        newImg.id = 'photoPreview';
                        newImg.className = previewContainer.className;
                        newImg.style.width = '150px';
                        newImg.style.height = '150px';
                        newImg.style.objectFit = 'cover';
                        // Copiar clases y estilos adicionales si existen
                        if (previewContainer.classList) {
                            previewContainer.classList.forEach(cls => newImg.classList.add(cls));
                        }
                        // Reemplazar el ícono por la nueva imagen
                        previewContainer.parentNode.replaceChild(newImg, previewContainer);
                        newImg.src = ev.target.result;
                    } else {
                        // Si ya es una imagen, solo actualizamos el src
                        previewContainer.src = ev.target.result;
                    }
                };
                reader.readAsDataURL(file);
            }
        });
    }

    // --- Confirmación de eliminación (no cambia) ---
    const deleteButtons = document.querySelectorAll('.delete-driver');
    if (deleteButtons.length) {
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
    }

    // --- Enfocar campo nombre (no cambia) ---
    const fullNameInput = document.getElementById('id_full_name');
    if (fullNameInput) fullNameInput.focus();
});