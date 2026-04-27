/*
  Web Scraper Agent — Frontend Logic
  Handles form submission, SSE streaming, table rendering, and file exports.
  No framework, no build step. Vanilla JS only.
*/

(function () {
  "use strict";

  // ── Element refs ──────────────────────────────────────────────────────────

  const urlInput      = document.getElementById("url-input");
  const queryInput    = document.getElementById("query-input");
  const runBtn        = document.getElementById("run-btn");
  const stopBtn       = document.getElementById("stop-btn");

  const logSection    = document.getElementById("log-section");
  const logBox        = document.getElementById("log-box");
  const statusBadge   = document.getElementById("status-badge");
  const pagesVisited  = document.getElementById("pages-visited");
  const pagesList     = document.getElementById("pages-list");

  const resultsSection = document.getElementById("results-section");
  const resultsMeta    = document.getElementById("results-meta");
  const tableHead      = document.getElementById("table-head");
  const tableBody      = document.getElementById("table-body");
  const reasoningBlock = document.getElementById("reasoning-block");
  const reasoningText  = document.getElementById("reasoning-text");

  const notFoundSection = document.getElementById("not-found-section");
  const notFoundReason  = document.getElementById("not-found-reason");

  // ── State ─────────────────────────────────────────────────────────────────

  let currentController = null;   // AbortController for the SSE fetch
  let lastResult = null;           // stored for export

  // ── Example buttons ───────────────────────────────────────────────────────

  document.querySelectorAll(".example-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      urlInput.value   = btn.dataset.url;
      queryInput.value = btn.dataset.query;
      urlInput.focus();
    });
  });

  // ── Run button ────────────────────────────────────────────────────────────

  runBtn.addEventListener("click", function () {
    var url   = urlInput.value.trim();
    var query = queryInput.value.trim();

    if (!url) {
      urlInput.focus();
      urlInput.style.borderColor = "#e05252";
      setTimeout(function () { urlInput.style.borderColor = ""; }, 1500);
      return;
    }

    if (!query) {
      queryInput.focus();
      queryInput.style.borderColor = "#e05252";
      setTimeout(function () { queryInput.style.borderColor = ""; }, 1500);
      return;
    }

    startRun(url, query);
  });

  // Also allow Ctrl+Enter in the textarea to trigger run
  queryInput.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      runBtn.click();
    }
  });

  // ── Stop button ───────────────────────────────────────────────────────────

  stopBtn.addEventListener("click", function () {
    if (currentController) {
      currentController.abort();
      currentController = null;
    }
    setStatus("stopped", "Stopped");
    stopBtn.style.display = "none";
    runBtn.disabled = false;
  });

  // ── Export buttons ────────────────────────────────────────────────────────

  document.querySelectorAll(".export-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (!lastResult || !lastResult.data || lastResult.data.length === 0) return;
      exportData(lastResult.data, btn.dataset.format);
    });
  });

  // ── Core run logic ────────────────────────────────────────────────────────

  function startRun(url, query) {
    // Reset UI
    resetResults();
    clearLog();
    logSection.style.display = "block";
    runBtn.disabled = true;
    stopBtn.style.display = "inline-block";
    setStatus("running", "Running");
    lastResult = null;

    logSection.scrollIntoView({ behavior: "smooth", block: "start" });

    currentController = new AbortController();

    fetch("/api/scrape/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url, query: query }),
      signal: currentController.signal,
    })
    .then(function (response) {
      if (!response.ok) {
        throw new Error("Server returned " + response.status);
      }
      return readSSEStream(response.body.getReader());
    })
    .catch(function (err) {
      if (err.name === "AbortError") return;  // user stopped — do nothing
      appendLog("[connection error] " + err.message, "step-error");
      setStatus("failed", "Error");
    })
    .finally(function () {
      runBtn.disabled = false;
      stopBtn.style.display = "none";
      currentController = null;
    });
  }

  // ── SSE stream reader ─────────────────────────────────────────────────────

  function readSSEStream(reader) {
    var buffer = "";

    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.done) return;

        buffer += new TextDecoder().decode(chunk.value, { stream: true });

        // SSE messages are separated by double newlines
        var parts = buffer.split("\n\n");
        // Last part may be incomplete — keep it in the buffer
        buffer = parts.pop();

        parts.forEach(function (part) {
          part = part.trim();
          if (!part || part.startsWith(":")) return;  // skip keepalive comments

          if (part.startsWith("data: ")) {
            var jsonStr = part.slice(6).trim();
            try {
              var msg = JSON.parse(jsonStr);
              handleMessage(msg);
            } catch (e) {
              // Malformed SSE data — ignore
            }
          }
        });

        return pump();
      });
    }

    return pump();
  }

  // ── Message handler ───────────────────────────────────────────────────────

  function handleMessage(msg) {
    if (msg.type === "log") {
      appendLog(msg.message);
    }

    else if (msg.type === "result") {
      lastResult = msg;

      if (msg.status === "success" && msg.data && msg.data.length > 0) {
        setStatus("done", "Done");
        renderTable(msg);
        if (msg.visited && msg.visited.length > 0) {
          pagesVisited.style.display = "block";
          pagesList.textContent = msg.visited.join(" > ");
        }
      } else {
        setStatus("failed", "Not found");
        showNotFound(msg.reasoning || "No relevant data found on the visited pages.");
      }
    }

    else if (msg.type === "error") {
      appendLog(msg.message, "step-error");
      setStatus("failed", "Error");
      showNotFound(msg.message);
    }

    else if (msg.type === "done") {
      runBtn.disabled = false;
      stopBtn.style.display = "none";
    }
  }

  // ── Log helpers ───────────────────────────────────────────────────────────

  function appendLog(text, forceClass) {
    var line = document.createElement("span");
    line.className = "log-line " + (forceClass || classifyLogLine(text));
    line.textContent = text;
    logBox.appendChild(line);
    logBox.appendChild(document.createTextNode("\n"));
    logBox.scrollTop = logBox.scrollHeight;
  }

  function classifyLogLine(text) {
    if (text.indexOf("[1/") !== -1 || text.indexOf("[2/") !== -1 ||
        text.indexOf("[3/") !== -1 || text.indexOf("Fetching") !== -1) {
      return "step-fetch";
    }
    if (text.indexOf("Asking LLM") !== -1) return "step-think";
    if (text.indexOf("Action=") !== -1)    return "step-action";
    if (text.indexOf("Done") !== -1 || text.indexOf("extracted") !== -1) return "step-done";
    if (text.indexOf("Already visited") !== -1 || text.indexOf("Redirect") !== -1 ||
        text.indexOf("navigate but gave") !== -1) return "step-warn";
    if (text.indexOf("Load error") !== -1 || text.indexOf("error") !== -1) return "step-error";
    return "";
  }

  function clearLog() {
    logBox.innerHTML = "";
  }

  // ── Status badge ──────────────────────────────────────────────────────────

  function setStatus(state, label) {
    statusBadge.textContent = label;
    statusBadge.className   = "status-badge " + state;
  }

  // ── Table rendering ───────────────────────────────────────────────────────

  function renderTable(result) {
    var data = result.data;
    if (!data || data.length === 0) return;

    var columns = Object.keys(data[0]);

    // Header
    var tr = document.createElement("tr");
    columns.forEach(function (col) {
      var th = document.createElement("th");
      th.textContent = col.replace(/_/g, " ");
      tr.appendChild(th);
    });
    tableHead.innerHTML = "";
    tableHead.appendChild(tr);

    // Rows
    tableBody.innerHTML = "";
    data.forEach(function (row) {
      var tr = document.createElement("tr");
      columns.forEach(function (col) {
        var td = document.createElement("td");
        var val = row[col] !== undefined ? String(row[col]) : "";

        if (col === "source_url" || col.toLowerCase().indexOf("url") !== -1 || col.toLowerCase().indexOf("link") !== -1) {
          td.className = "url-cell";
          if (val.startsWith("http")) {
            var a = document.createElement("a");
            a.href   = val;
            a.target = "_blank";
            a.rel    = "noopener";
            a.textContent = shortenUrl(val);
            td.appendChild(a);
          } else {
            td.textContent = val;
          }
        } else {
          td.textContent = val;
        }

        tr.appendChild(td);
      });
      tableBody.appendChild(tr);
    });

    // Meta line
    var pageCount = result.visited ? result.visited.length : 0;
    resultsMeta.textContent =
      data.length + " item" + (data.length !== 1 ? "s" : "") +
      " extracted across " +
      pageCount + " page" + (pageCount !== 1 ? "s" : "");

    // Reasoning
    if (result.reasoning) {
      reasoningText.textContent = result.reasoning;
      reasoningBlock.style.display = "flex";
    }

    resultsSection.style.display = "block";
    resultsSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function shortenUrl(url) {
    try {
      var parsed = new URL(url);
      var path   = parsed.pathname;
      if (path.length > 40) {
        path = "..." + path.slice(-38);
      }
      return parsed.hostname + path;
    } catch (e) {
      return url.length > 55 ? url.slice(0, 52) + "..." : url;
    }
  }

  // ── Not found ─────────────────────────────────────────────────────────────

  function showNotFound(reason) {
    notFoundReason.textContent = reason;
    notFoundSection.style.display = "block";
    notFoundSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ── Reset ─────────────────────────────────────────────────────────────────

  function resetResults() {
    resultsSection.style.display    = "none";
    notFoundSection.style.display   = "none";
    pagesVisited.style.display      = "none";
    reasoningBlock.style.display    = "none";
    tableHead.innerHTML             = "";
    tableBody.innerHTML             = "";
    resultsMeta.textContent         = "";
    reasoningText.textContent       = "";
    notFoundReason.textContent      = "";
    pagesList.textContent           = "";
  }

  // ── Export ────────────────────────────────────────────────────────────────

  function exportData(data, format) {
    fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        data: data,
        format: format,
        filename: "scraper_results",
      }),
    })
    .then(function (response) {
      if (!response.ok) throw new Error("Export failed");
      return response.blob().then(function (blob) {
        var ext = format === "excel" ? "xlsx" : format;
        var name = "scraper_results." + ext;
        var url  = URL.createObjectURL(blob);
        var a    = document.createElement("a");
        a.href   = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      });
    })
    .catch(function (err) {
      alert("Export failed: " + err.message);
    });
  }

})();
