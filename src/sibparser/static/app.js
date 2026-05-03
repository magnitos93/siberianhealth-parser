// Tiny vanilla-JS UI for the parser. No build step.
(() => {
    const $ = (sel) => document.querySelector(sel);
    const logEl = $("#log");
    const treeEl = $("#tree");
    const driveStatus = $("#drive-status");
    const credentialsInput = $("#credentials-path");
    const filterInput = $("#filter");

    function append(line, kind) {
        const el = document.createElement("div");
        el.className = `log-${kind || "info"}`;
        el.textContent = line;
        logEl.appendChild(el);
        logEl.scrollTop = logEl.scrollHeight;
    }

    function api(path, body) {
        const opts = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
        return fetch(path, opts).then(async (r) => {
            const data = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(data.detail || r.statusText);
            return data;
        });
    }

    async function refreshStatus() {
        try {
            const s = await api("/api/status");
            const parts = [];
            parts.push(s.credentials_present ? "✓ credentials.json" : "✗ credentials.json");
            parts.push(s.token_present ? "✓ token" : "✗ token");
            parts.push(s.drive_authorized ? "✓ авторизован" : "не авторизован");
            driveStatus.textContent = parts.join(" · ");
            driveStatus.style.color = s.drive_authorized ? "var(--good)" : "var(--muted)";
        } catch (err) {
            driveStatus.textContent = `Ошибка: ${err.message}`;
        }
    }

    function selectedCategoryPaths() {
        return Array.from(document.querySelectorAll("input.cat-check:checked")).map((el) => el.dataset.path);
    }

    function renderTree(tree) {
        treeEl.innerHTML = "";
        if (!tree || !tree.length) {
            treeEl.innerHTML = "<p class='muted'>Дерево не загружено. Нажми «Открыть каталог» слева.</p>";
            return;
        }
        for (const top of tree) {
            const group = document.createElement("div");
            group.className = "tree-group";
            const title = document.createElement("label");
            title.className = "group-title";
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.className = "cat-check group-check";
            cb.dataset.path = top.path;
            const span = document.createElement("span");
            span.textContent = top.name + (top.children && top.children.length ? ` (${top.children.length})` : "");
            title.appendChild(cb);
            title.appendChild(span);
            group.appendChild(title);

            if (top.children && top.children.length) {
                const ch = document.createElement("div");
                ch.className = "tree-children";
                for (const child of top.children) {
                    const leaf = document.createElement("label");
                    leaf.className = "tree-leaf";
                    const lcb = document.createElement("input");
                    lcb.type = "checkbox";
                    lcb.className = "cat-check leaf-check";
                    lcb.dataset.path = child.path;
                    const txt = document.createElement("span");
                    txt.textContent = child.name;
                    const link = document.createElement("a");
                    link.href = child.url || "#";
                    link.target = "_blank";
                    link.textContent = "↗";
                    leaf.appendChild(lcb);
                    leaf.appendChild(txt);
                    if (child.url) leaf.appendChild(link);
                    ch.appendChild(leaf);
                }
                group.appendChild(ch);

                cb.addEventListener("change", () => {
                    ch.querySelectorAll("input.leaf-check").forEach((c) => (c.checked = cb.checked));
                });
            }
            treeEl.appendChild(group);
        }
        applyFilter();
    }

    function applyFilter() {
        const q = filterInput.value.trim().toLowerCase();
        for (const grp of treeEl.querySelectorAll(".tree-group")) {
            let anyVisible = false;
            for (const leaf of grp.querySelectorAll(".tree-leaf")) {
                const text = leaf.textContent.toLowerCase();
                const visible = !q || text.includes(q);
                leaf.style.display = visible ? "" : "none";
                if (visible) anyVisible = true;
            }
            const titleText = grp.querySelector(".group-title").textContent.toLowerCase();
            if (q && titleText.includes(q)) anyVisible = true;
            grp.style.display = anyVisible || !q ? "" : "none";
        }
    }

    filterInput.addEventListener("input", applyFilter);

    // -- WebSocket -----------------------------------------------------
    function connectWs() {
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        const ws = new WebSocket(`${proto}//${location.host}/ws/progress`);
        ws.onmessage = (ev) => {
            try {
                const e = JSON.parse(ev.data);
                append(e.message, e.kind);
            } catch {
                append(ev.data, "info");
            }
        };
        ws.onclose = () => setTimeout(connectWs, 1500);
        ws.onerror = () => append("WebSocket ошибка, переподключение…", "error");
    }

    // -- Buttons -------------------------------------------------------
    $("#auth-btn").addEventListener("click", async () => {
        try {
            append("Авторизация Google Drive…", "info");
            const path = credentialsInput.value || null;
            await api("/api/auth/drive", { credentials_path: path });
            append("Авторизация прошла успешно", "done");
            refreshStatus();
        } catch (err) {
            append(`Авторизация: ${err.message}`, "error");
        }
    });

    $("#discover-btn").addEventListener("click", async () => {
        try {
            append("Открываю каталог в браузере…", "info");
            const data = await api("/api/discover", {});
            renderTree(data.tree);
            append(`Дерево загружено: ${data.tree.length} разделов`, "done");
        } catch (err) {
            append(`Discover: ${err.message}`, "error");
        }
    });

    $("#run-btn").addEventListener("click", async () => {
        const paths = selectedCategoryPaths();
        if (!paths.length) { append("Сначала выбери категории галочками", "error"); return; }
        const limit = parseInt($("#limit").value, 10) || 0;
        const upload = $("#upload-to-drive").checked;
        try {
            await api("/api/run", {
                selected_category_paths: paths,
                products_per_category_limit: limit,
                upload_to_drive: upload,
            });
            append(`Запущено для ${paths.length} категорий (лимит ${limit || "∞"})`, "info");
        } catch (err) {
            append(`Запуск: ${err.message}`, "error");
        }
    });

    $("#single-run-btn").addEventListener("click", async () => {
        const url = $("#single-product").value.trim();
        if (!url) { append("Введи URL товара", "error"); return; }
        const upload = $("#upload-to-drive").checked;
        try {
            await api("/api/run", {
                single_product_url: url,
                upload_to_drive: upload,
            });
            append(`Запущено для одного товара: ${url}`, "info");
        } catch (err) {
            append(`Запуск: ${err.message}`, "error");
        }
    });

    $("#cancel-btn").addEventListener("click", async () => {
        await api("/api/cancel", {});
        append("Отмена…", "info");
    });

    // -- init ----------------------------------------------------------
    refreshStatus();
    connectWs();
})();
