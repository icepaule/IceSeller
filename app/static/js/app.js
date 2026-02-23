/* IceSeller - Main JS */

function showSpinner(message) {
    const overlay = document.getElementById('spinner-overlay');
    if (overlay) {
        const msg = overlay.querySelector('.spinner-message');
        if (msg) msg.textContent = message || 'Bitte warten...';
        overlay.classList.add('active');
    }
}

function hideSpinner() {
    const overlay = document.getElementById('spinner-overlay');
    if (overlay) overlay.classList.remove('active');
}

function showToast(message, type) {
    type = type || 'info';
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = 'toast align-items-center text-bg-' + type + ' border-0 show';
    toast.setAttribute('role', 'alert');
    toast.innerHTML =
        '<div class="d-flex">' +
            '<div class="toast-body">' + message + '</div>' +
            '<button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>' +
        '</div>';
    container.appendChild(toast);
    setTimeout(function() { toast.remove(); }, 5000);
}

async function apiCall(url, options) {
    options = options || {};
    try {
        const response = await fetch(url, options);
        if (!response.ok) {
            const err = await response.json().catch(function() { return {detail: response.statusText}; });
            throw new Error(err.detail || 'API Error');
        }
        return await response.json();
    } catch (e) {
        showToast('Fehler: ' + e.message, 'danger');
        throw e;
    }
}

/* Camera functions */
function capturePhoto() {
    var itemId = document.getElementById('item-id');
    var itemIdVal = itemId ? itemId.value : '';
    showSpinner('Foto wird aufgenommen...');

    apiCall('/camera/capture-photo', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({item_id: itemIdVal || null})
    }).then(function(data) {
        hideSpinner();
        if (data.item_id && itemId) itemId.value = data.item_id;
        addCapturedThumb(data.image_path, data.item_id);
        showToast('Foto aufgenommen!', 'success');
    }).catch(function() {
        hideSpinner();
    });
}

function addCapturedThumb(imagePath, itemId) {
    var container = document.getElementById('captured-images');
    if (!container) return;

    var img = document.createElement('img');
    img.className = 'thumb';
    img.src = '/data/images/' + imagePath;
    container.appendChild(img);

    var btn = document.getElementById('btn-identify');
    if (btn) {
        btn.disabled = false;
        btn.href = '/identify/' + itemId;
    }
}

function ptzControl(direction) {
    apiCall('/camera/ptz', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({direction: direction})
    });
}
