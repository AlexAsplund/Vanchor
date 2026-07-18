/* Vanchor-NG — System & updates card (adoption task 5).
 *
 * Drives the #system-card via telemetry.supervisor from the runtime's
 * supervisor link.  When the supervisor is not detected the card shows
 * version info only.  All network calls use VA.postJSON / VA.getJSON.
 *
 * Update choreography (§6.6 of the brief):
 *   verify → backup → load phases: live telemetry job updates
 *   recreate phase (or WS drop while job active): show reconnect banner,
 *   poll /api/state, then read /api/supervisor/proxy/v1/jobs/last.
 */
"use strict";

(function () {
  // Guard: only run if the system card and VA are present.
  if (!document.getElementById("system-card") || !window.VA) return;

  const $ = (id) => document.getElementById(id);

  // ---- PEP-440-alpha comparator (mirrors versionspec.py §5.3) -----------
  const PRE = { a: 0, b: 1, rc: 2 };
  function parseVer(s) {
    const m = /^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$/.exec(String(s).trim());
    if (!m) return null;
    return [+m[1], +m[2], +m[3], m[4] ? (PRE[m[4]] ?? 2) : 3, m[5] ? +m[5] : 0];
  }
  function verGt(a, b) {
    const pa = parseVer(a), pb = parseVer(b);
    if (!pa || !pb) return false;
    for (let i = 0; i < 5; i++) {
      if (pa[i] > pb[i]) return true;
      if (pa[i] < pb[i]) return false;
    }
    return false;
  }

  // ---- safe DOM helpers --------------------------------------------------
  function setText(id, val) {
    const el = $(id);
    if (el && el.textContent !== val) el.textContent = val;
  }
  function show(id, visible) {
    const el = $(id);
    if (el) el.style.display = visible ? "" : "none";
  }
  function enable(id, on) {
    const el = $(id);
    if (el) el.disabled = !on;
  }

  // ---- disk-crit banner (reuses health.js pattern) -----------------------
  let diskBanner = null;
  function ensureDiskBanner() {
    if (diskBanner) return;
    diskBanner = document.createElement("div");
    diskBanner.id = "sup-disk-crit-banner";
    diskBanner.className = "sbanner sbanner-alarm hidden";
    diskBanner.setAttribute("role", "alert");
    const msg = document.createElement("span");
    msg.className = "sb-msg";
    msg.textContent = "Boat disk nearly full — open Settings › Data";
    diskBanner.appendChild(msg);
    const container = document.getElementById("safety-banners");
    if (container) container.appendChild(diskBanner);
  }

  // ---- state -------------------------------------------------------------
  let _bundleName = null;   // after successful upload (passed to inspect/apply)
  let _manifestData = null; // last inspect result
  let _appVersion = null;   // from telemetry
  let _supAvailable = false;
  let _activeJob = null;    // from telemetry while WS alive
  let _reconnecting = false;

  // ---- telemetry subscriber ---------------------------------------------
  VA.onTelemetry(function (t) {
    const sup = (t && t.supervisor) || null;
    _appVersion = sup ? sup.app_version : null;
    _supAvailable = !!(sup && sup.available);
    _activeJob = sup ? sup.job : null;

    // Version lines
    setText("sys-version-line", _appVersion || "—");
    if (!_supAvailable) {
      setText("sys-sup-line", "supervisor not detected — running bare-metal?");
      show("sys-supervisor-sections", false);
    } else {
      setText("sys-sup-line", sup.supervisor_version || "—");
      show("sys-supervisor-sections", true);
    }

    // Disk banner (disk_crit)
    ensureDiskBanner();
    const isCrit = !!(sup && sup.warnings && sup.warnings.includes("disk_crit"));
    if (diskBanner) diskBanner.classList.toggle("hidden", !isCrit);

    // Disk line
    if (sup && sup.disk) {
      const d = sup.disk;
      const pct = d.data_used_pct != null ? d.data_used_pct.toFixed(1) + "%" : "—";
      const imgGb = d.docker_images_bytes != null
        ? (d.docker_images_bytes / 1e9).toFixed(2) + " GB images"
        : "";
      const reclaim = d.docker_reclaimable_bytes != null
        ? " (" + (d.docker_reclaimable_bytes / 1e9).toFixed(2) + " GB reclaimable)"
        : "";
      setText("sys-disk-line", pct + " used" + (imgGb ? " · " + imgGb + reclaim : ""));
    }

    // Active job (while WS alive and no reconnect flow)
    if (!_reconnecting) {
      renderJob(sup ? sup.job : null);
    }

    // Last job (when no active job)
    if (!sup?.job && sup?.last_job) {
      renderLastJob(sup.last_job);
    }

    // Backup list
    if (sup && sup.backups) {
      setText("sys-backup-list", ""); // will be rendered by renderBackups
    }

    // Rollback button: only available when previous_tag exists
    enable("sys-rollback-btn", !!(sup && sup.previous_tag));
  });

  function renderJob(job) {
    if (!job) {
      setText("sys-job-line", "No active job.");
      return;
    }
    const pct = job.progress_pct != null ? " " + job.progress_pct + "%" : "";
    setText("sys-job-line",
      "[" + job.phase + "]" + pct + " " + (job.detail || ""));
    // If recreate phase: start reconnect choreography
    if (job.phase === "recreate" && !_reconnecting) {
      startReconnectChoreography(job);
    }
  }

  function renderLastJob(job) {
    if (!job) { setText("sys-lastjob-line", ""); return; }
    let msg = "Last: ";
    if (job.ok && !job.rolled_back) {
      msg += "✅ Updated to " + job.to_tag;
    } else if (job.rolled_back) {
      msg += "❌ Update failed — rolled back to " + job.from_tag;
      if (job.error) msg += " (" + job.error + ")";
    } else if (job.ok === false) {
      msg += "❌ Failed: " + (job.error || job.detail || "unknown");
    } else {
      msg += job.phase + " " + (job.detail || "");
    }
    setText("sys-lastjob-line", msg);
  }

  // ---- reconnect choreography (§6.6) ------------------------------------
  function startReconnectChoreography(job) {
    _reconnecting = true;
    setText("sys-job-line", "Restarting vanchor — this page will reconnect…");
    // Poll /api/state every 2s until it returns 200
    let pollTimer = setInterval(function () {
      fetch("/api/state")
        .then(function (r) {
          if (r.ok) {
            clearInterval(pollTimer);
            // Fetch last job result
            VA.getJSON("/api/supervisor/proxy/v1/jobs/last")
              .then(function (lastJob) {
                _reconnecting = false;
                if (lastJob && lastJob.phase === "done" && lastJob.ok) {
                  setText("sys-job-line", "");
                  renderLastJob(lastJob);
                } else if (lastJob && lastJob.rolled_back) {
                  setText("sys-job-line", "");
                  renderLastJob(lastJob);
                } else if (lastJob && lastJob.job) {
                  // Still running (edge case)
                  _activeJob = lastJob;
                }
              })
              .catch(function () { _reconnecting = false; });
          }
        })
        .catch(function () { /* keep polling */ });
    }, 2000);
  }

  // ---- check for updates (GitHub releases feed) -------------------------
  $("sys-check-btn") && $("sys-check-btn").addEventListener("click", function () {
    setText("sys-check-status", "Checking…");
    show("sys-latest-row", false);
    fetch("https://api.github.com/repos/AlexAsplund/Vanchor/releases?per_page=15")
      .then(function (r) { return r.json(); })
      .then(function (releases) {
        // Find the latest release with an arm64 bundle asset
        let latest = null;
        for (const rel of releases) {
          if (!rel.tag_name) continue;
          const hasBundle = (rel.assets || []).some(function (a) {
            return a.name && a.name.startsWith("vanchor-app-") && a.name.endsWith("-arm64.bundle.tar");
          });
          if (!hasBundle) continue;
          const ver = rel.tag_name.replace(/^v/, "");
          if (!latest || verGt(ver, latest.ver)) {
            const asset = rel.assets.find(function (a) {
              return a.name.startsWith("vanchor-app-") && a.name.endsWith("-arm64.bundle.tar");
            });
            latest = { ver, tag: rel.tag_name, asset };
          }
        }
        if (!latest) {
          setText("sys-check-status", "No arm64 bundle release found.");
          return;
        }
        const currentVer = _appVersion;
        const isNewer = currentVer ? verGt(latest.ver, currentVer) : true;
        setText("sys-check-status",
          isNewer ? ("Update available: v" + latest.ver) : "Already up to date (v" + latest.ver + ")");
        setText("sys-latest-line", "v" + latest.ver + (isNewer ? " ★ new" : ""));
        const link = $("sys-download-link");
        if (link && latest.asset) {
          link.href = latest.asset.browser_download_url;
          link.download = latest.asset.name;
          link.hidden = false;
        }
        show("sys-latest-row", true);
      })
      .catch(function (err) {
        setText("sys-check-status", "Check failed: " + err.message);
      });
  });

  // ---- upload (chunked) -------------------------------------------------
  const CHUNK = 8 * 1024 * 1024; // 8 MB

  $("sys-upload-file") && $("sys-upload-file").addEventListener("change", function () {
    const f = this.files && this.files[0];
    enable("sys-upload-btn", !!f);
    enable("sys-apply-btn", false);
    _bundleName = null;
    _manifestData = null;
    setText("sys-upload-status", f ? f.name : "");
    show("sys-compat-warning", false);
  });

  $("sys-upload-btn") && $("sys-upload-btn").addEventListener("click", function () {
    const input = $("sys-upload-file");
    const f = input && input.files && input.files[0];
    if (!f) return;
    const name = f.name.replace(/[^A-Za-z0-9._\-]/g, "_");
    if (!name.endsWith(".bundle.tar")) {
      setText("sys-upload-status", "File must end in .bundle.tar");
      return;
    }
    enable("sys-upload-btn", false);
    const prog = $("sys-upload-progress");
    if (prog) { prog.style.display = ""; prog.value = 0; }
    setText("sys-upload-status", "Uploading…");

    let offset = 0;
    function uploadChunk() {
      const isDone = offset + CHUNK >= f.size;
      const chunk = f.slice(offset, offset + CHUNK);
      const url = "/api/supervisor/upload?name=" + encodeURIComponent(name)
        + "&offset=" + offset + "&done=" + (isDone ? 1 : 0);
      fetch(url, { method: "POST", body: chunk })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.error === "bad_offset") {
            // Resume from server's current size
            offset = data.size;
            uploadChunk();
            return;
          }
          if (!data.ok) {
            setText("sys-upload-status", "Upload error: " + (data.error || "unknown"));
            enable("sys-upload-btn", true);
            if (prog) prog.style.display = "none";
            return;
          }
          offset = data.size;
          if (prog) prog.value = offset / f.size;
          if (isDone) {
            _bundleName = data.bundle;
            setText("sys-upload-status", "Upload complete. Inspecting…");
            if (prog) prog.style.display = "none";
            inspectBundle();
          } else {
            uploadChunk();
          }
        })
        .catch(function (err) {
          setText("sys-upload-status", "Upload error: " + err.message);
          enable("sys-upload-btn", true);
          if (prog) prog.style.display = "none";
        });
    }
    uploadChunk();
  });

  function inspectBundle() {
    if (!_bundleName) return;
    VA.postJSON("/api/supervisor/proxy/v1/update/inspect", { bundle: _bundleName })
      .then(function (data) {
        _manifestData = data;
        show("sys-compat-warning", false);
        if (data.compatible) {
          const ver = (data.manifest && data.manifest.app_version) || "?";
          setText("sys-upload-status",
            "Ready to apply: v" + ver + " (current: v" + (data.current_tag || "?") + ")");
          enable("sys-apply-btn", true);
        } else {
          const reason = data.reason || "incompatible";
          let warn = "Cannot apply: " + reason;
          if (reason && (reason.startsWith("supervisor_too_old") ||
              data.api_version_mismatch)) {
            warn = "This update needs a newer supervisor — update the supervisor first.";
          }
          setText("sys-compat-warning", warn);
          show("sys-compat-warning", true);
          setText("sys-upload-status", "Incompatible bundle.");
          enable("sys-apply-btn", false);
        }
      })
      .catch(function (err) {
        setText("sys-upload-status", "Inspect failed: " + err.message);
      });
  }

  // ---- apply update -----------------------------------------------------
  $("sys-apply-btn") && $("sys-apply-btn").addEventListener("click", function () {
    if (!_bundleName) return;
    if (!confirm("Apply this update? The app will restart. If health checks fail, it rolls back automatically.")) return;
    VA.postJSON("/api/supervisor/proxy/v1/update/apply", {
      name: "vanchor",
      source: "bundle",
      bundle: _bundleName,
    })
      .then(function (data) {
        if (data.job_id) {
          setText("sys-job-line", "Job started: " + data.job_id);
          enable("sys-apply-btn", false);
        } else if (data.error === "busy") {
          setText("sys-upload-status", "Another job is running — try again shortly.");
        } else {
          setText("sys-upload-status", "Apply error: " + (data.error || JSON.stringify(data)));
        }
      })
      .catch(function (err) {
        setText("sys-upload-status", "Apply error: " + err.message);
      });
  });

  // ---- rollback ---------------------------------------------------------
  $("sys-rollback-btn") && $("sys-rollback-btn").addEventListener("click", function () {
    if (!confirm("Roll back to the previous version?")) return;
    VA.postJSON("/api/supervisor/proxy/v1/rollback", { name: "vanchor" })
      .then(function (data) {
        if (data.job_id) {
          setText("sys-job-line", "Rollback started: " + data.job_id);
        } else {
          setText("sys-job-line", "Rollback error: " + (data.error || JSON.stringify(data)));
        }
      })
      .catch(function (err) {
        setText("sys-job-line", "Rollback error: " + err.message);
      });
  });

  // ---- backup now -------------------------------------------------------
  $("sys-backup-now-btn") && $("sys-backup-now-btn").addEventListener("click", function () {
    VA.postJSON("/api/supervisor/proxy/v1/backup", {})
      .then(function (data) {
        if (data.job_id) {
          setText("sys-job-line", "Backup job started: " + data.job_id);
        } else {
          setText("sys-job-line", "Backup error: " + (data.error || JSON.stringify(data)));
        }
      })
      .catch(function (err) {
        setText("sys-job-line", "Backup error: " + err.message);
      });
  });

  // ---- prune ------------------------------------------------------------
  $("sys-prune-btn") && $("sys-prune-btn").addEventListener("click", function () {
    VA.postJSON("/api/supervisor/proxy/v1/prune", {})
      .then(function (data) {
        if (data.job_id) {
          setText("sys-job-line", "Prune job started: " + data.job_id);
        }
      })
      .catch(function (err) {
        setText("sys-job-line", "Prune error: " + err.message);
      });
  });

})();
