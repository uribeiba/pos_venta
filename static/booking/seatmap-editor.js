// seatmap-editor.js – Editor PRO con drag painting y numeración inteligente
let currentTool = "L";
let mouseDown = false;
let grid = [];
let rows, cols;
let prefixLower = "", prefixUpper = "";
let currentDeck = 1; // 1 = inferior, 2 = superior (si existe)
let hasUpperDeck = false;

const el = document.getElementById("editorGrid");
const toolButtons = document.querySelectorAll(".tool-btn");
const deckButtons = document.querySelectorAll(".deck-btn");
const saveBtn = document.getElementById("saveLayoutBtn");

function setTool(t) {
    currentTool = t;
    toolButtons.forEach(btn => {
        btn.classList.toggle("active", btn.getAttribute("data-tool") === t);
    });
}

function setDeck(deck) {
    currentDeck = deck;
    deckButtons.forEach(btn => {
        btn.classList.toggle("active", parseInt(btn.getAttribute("data-deck")) === deck);
    });
    render();
}

function getColor(t) {
    const colors = {
        L: "#22c55e",
        P: "transparent",
        X: "#e5e7eb",
        E: "#f59e0b",
        D: "#3b82f6",
        B: "#ec4899"
    };
    return colors[t] || "#ddd";
}

function getSymbol(t) {
    const symbols = {
        L: "🪑",
        P: "⬚",
        X: "❌",
        E: "⬆",
        D: "🚪",
        B: "🚽"
    };
    return symbols[t] || "";
}

// Numeración inteligente: solo celdas tipo "L" reciben número secuencial por piso
function computeNumbers(layout, deck) {
    let counter = 1;
    const prefix = (deck === 1 ? prefixLower : prefixUpper) || "";
    return layout.map(cell => {
        if (cell === "L") {
            let num = `${prefix}${counter}`;
            counter++;
            return num;
        }
        return "";
    });
}

function render() {
    if (!el) return;
    el.style.gridTemplateColumns = `repeat(${cols}, 56px)`;
    el.innerHTML = "";

    const currentLayout = (currentDeck === 1) ? CONFIG.layout_lower : CONFIG.layout_upper;
    if (!currentLayout || currentLayout.length === 0) return;

    const numbers = computeNumbers(currentLayout, currentDeck);

    currentLayout.forEach((cell, idx) => {
        const div = document.createElement("div");
        div.className = `cell cell--${cell}`;
        div.style.backgroundColor = getColor(cell);
        div.style.display = "flex";
        div.style.flexDirection = "column";
        div.style.alignItems = "center";
        div.style.justifyContent = "center";
        div.style.fontSize = "11px";
        div.style.fontWeight = "bold";

        const symbolSpan = document.createElement("span");
        symbolSpan.textContent = getSymbol(cell);
        symbolSpan.style.fontSize = "18px";
        div.appendChild(symbolSpan);

        if (cell === "L" && numbers[idx]) {
            const numSpan = document.createElement("span");
            numSpan.textContent = numbers[idx];
            numSpan.style.fontSize = "10px";
            numSpan.style.marginTop = "2px";
            numSpan.style.backgroundColor = "rgba(0,0,0,0.1)";
            numSpan.style.padding = "0 3px";
            numSpan.style.borderRadius = "8px";
            div.appendChild(numSpan);
        }

        div.onmousedown = (e) => {
            e.preventDefault();
            mouseDown = true;
            paintCell(idx);
        };
        div.onmouseover = () => {
            if (mouseDown) paintCell(idx);
        };
        el.appendChild(div);
    });
}

function paintCell(idx) {
    if (currentDeck === 1) {
        CONFIG.layout_lower[idx] = currentTool;
    } else {
        CONFIG.layout_upper[idx] = currentTool;
    }
    render();
}

function init() {
    rows = CONFIG.rows;
    cols = CONFIG.cols;
    prefixLower = CONFIG.prefix_lower || "";
    prefixUpper = CONFIG.prefix_upper || "";
    hasUpperDeck = CONFIG.has_upper || false;

    if (!CONFIG.layout_lower || CONFIG.layout_lower.length === 0) {
        CONFIG.layout_lower = new Array(rows * cols).fill("L");
    }
    if (hasUpperDeck && (!CONFIG.layout_upper || CONFIG.layout_upper.length === 0)) {
        CONFIG.layout_upper = new Array(CONFIG.rows_upper * cols).fill("L");
    }
    render();

    // Toolbar events
    toolButtons.forEach(btn => {
        btn.addEventListener("click", () => setTool(btn.getAttribute("data-tool")));
    });
    deckButtons.forEach(btn => {
        btn.addEventListener("click", () => setDeck(parseInt(btn.getAttribute("data-deck"))));
    });

    // Drag global
    document.body.onmouseup = () => mouseDown = false;
    document.body.ondragstart = (e) => e.preventDefault();
}

function saveLayout() {
    fetch(CONFIG.save_url, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": getCSRF()
        },
        body: JSON.stringify({
            layout_lower: CONFIG.layout_lower,
            layout_upper: CONFIG.layout_upper,
            rows_lower: rows,
            rows_upper: CONFIG.rows_upper || 0,
            cols: cols
        })
    }).then(res => res.json()).then(data => {
        if (data.status === "ok") {
            alert("✅ Layout guardado correctamente");
        } else {
            alert("❌ Error: " + (data.message || "desconocido"));
        }
    }).catch(err => alert("Error de red: " + err));
}

function getCSRF() {
    return document.cookie.split('; ')
        .find(row => row.startsWith('csrftoken'))
        ?.split('=')[1];
}

init();
saveBtn?.addEventListener("click", saveLayout);