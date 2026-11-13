document.addEventListener("DOMContentLoaded", () => {
  const root = document.documentElement;
  const themeToggle = document.querySelector("[data-theme-toggle]");

  function applyTheme(theme) {
    root.dataset.theme = theme;
    const dark = theme === "dark";
    if (themeToggle) {
      themeToggle.innerHTML = dark
        ? '<i class="fa-solid fa-sun" aria-hidden="true"></i><span class="visually-hidden">Switch to light theme</span>'
        : '<i class="fa-solid fa-moon" aria-hidden="true"></i><span class="visually-hidden">Switch to dark theme</span>';
      themeToggle.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
      themeToggle.title = dark ? "Switch to light theme" : "Switch to dark theme";
    }
    try {
      localStorage.setItem("smartpark-theme", theme);
    } catch {}
    window.dispatchEvent(new CustomEvent("smartpark:theme", {detail: {theme}}));
  }

  if (themeToggle) {
    applyTheme(root.dataset.theme === "dark" ? "dark" : "light");
    themeToggle.addEventListener("click", () => {
      applyTheme(root.dataset.theme === "dark" ? "light" : "dark");
    });
  }

  const currentPath = window.location.pathname.replace(/\/+$/, "") || "/";
  document.querySelectorAll(".site-nav .nav-link").forEach(link => {
    try {
      const linkUrl = new URL(link.href, window.location.origin);
      if (linkUrl.origin === window.location.origin &&
          linkUrl.pathname.replace(/\/+$/, "") === currentPath) {
        link.setAttribute("aria-current", "page");
      }
    } catch {}
  });

  const revealItems = document.querySelectorAll(
    "[data-aos], .hero-copy, .hero-visual, .metric-card, .panel, .feature-card, .location-card, .auth-card, .ticket-card, .search-panel, .page-hero .container"
  );
  if ("IntersectionObserver" in window && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    root.classList.add("reveal-ready");
    const revealObserver = new IntersectionObserver((entries, observer) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      });
    }, {threshold: 0.08, rootMargin: "0px 0px -24px 0px"});
    revealItems.forEach((item, index) => {
      item.classList.add("reveal-item");
      item.style.transitionDelay = `${Math.min(index % 4, 3) * 55}ms`;
      revealObserver.observe(item);
    });
  }

  if ("IntersectionObserver" in window) {
    const counterObserver = new IntersectionObserver((entries, observer) => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        const element = entry.target;
        observer.unobserve(element);
        if (element.dataset.counted) return;
        element.dataset.counted = "true";
        const original = element.textContent.trim();
        const match = original.match(/^([^\d]*)([\d,]+(?:\.\d+)?)(.*)$/);
        if (!match) return;
        const start = Number(match[2].replace(/,/g, ""));
        if (!Number.isFinite(start) || start === 0) return;
        const decimals = (match[2].split(".")[1] || "").length;
        const duration = 720;
        const startedAt = performance.now();
        const render = now => {
          const progress = Math.min((now - startedAt) / duration, 1);
          const eased = 1 - Math.pow(1 - progress, 3);
          const current = start * eased;
          const formatted = new Intl.NumberFormat(undefined, {
            minimumFractionDigits: decimals,
            maximumFractionDigits: decimals
          }).format(progress === 1 ? start : current);
          element.textContent = `${match[1]}${formatted}${match[3]}`;
          if (progress < 1) requestAnimationFrame(render);
        };
        requestAnimationFrame(render);
      });
    }, {threshold: 0.5});
    document.querySelectorAll(".metric-card strong, .stats-row strong").forEach(item => counterObserver.observe(item));
  }

  const slots = document.querySelectorAll(".parking-slot:not(:disabled)");
  const selectedInput = document.getElementById("selected-slot");
  const selectedLabel = document.getElementById("selected-label");
  slots.forEach(slot => {
    slot.setAttribute("aria-pressed", "false");
    slot.addEventListener("click", () => {
      slots.forEach(item => {
        item.classList.remove("is-selected");
        item.setAttribute("aria-pressed", "false");
      });
      slot.classList.add("is-selected");
      slot.setAttribute("aria-pressed", "true");
      if (selectedInput) selectedInput.value = slot.dataset.slotId;
      if (selectedLabel) {
        selectedLabel.textContent = `Selected space ${slot.querySelector("span")?.textContent || ""}`;
        selectedLabel.classList.remove("text-danger");
      }
    });
  });

  const bookingForm = document.querySelector(".booking-form");
  if (bookingForm && selectedInput) {
    bookingForm.addEventListener("submit", event => {
      if (!selectedInput.value) {
        event.preventDefault();
        if (selectedLabel) {
          selectedLabel.textContent = "Choose an available space to continue.";
          selectedLabel.classList.add("text-danger");
        }
        return;
      }
      const submit = bookingForm.querySelector('button[type="submit"], button:not([type])');
      if (submit) {
        submit.disabled = true;
        submit.setAttribute("aria-busy", "true");
        submit.innerHTML = '<span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>Preparing your booking…';
      }
    });
  }

  const availability = document.querySelector(".slot-grid");
  if (availability && window.smartparkLocationId && window.WebSocket) {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    let socket;
    let reconnectDelay = 1500;
    let reconnectTimer;
    let reloadTimer;
    let stopped = false;
    const connect = () => {
      if (stopped) return;
      socket = new WebSocket(`${protocol}//${window.location.host}/ws/availability/`);
      socket.addEventListener("open", () => { reconnectDelay = 1500; });
      socket.addEventListener("message", () => {
        if (!reloadTimer) {
          if (typeof window.showSmartParkToast === "function") {
            window.showSmartParkToast("Parking availability updated. Refreshing spaces…");
          }
          reloadTimer = window.setTimeout(() => window.location.reload(), 650);
        }
      });
      socket.addEventListener("close", scheduleReconnect);
      socket.addEventListener("error", event => {
        console.error("SmartPark availability WebSocket error", event);
        socket.close();
      });
    };
    const scheduleReconnect = () => {
      if (stopped || reconnectTimer) return;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 30000);
    };
    connect();
    window.addEventListener("pagehide", () => {
      stopped = true;
      window.clearTimeout(reconnectTimer);
      window.clearTimeout(reloadTimer);
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close();
    }, {once: true});
  }
});

window.showSmartParkToast = message => {
  const container = document.getElementById("smartpark-toasts");
  if (!container) return;
  const toast = document.createElement("div");
  toast.className = "smartpark-toast";
  toast.setAttribute("role", "status");
  toast.textContent = message;
  container.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add("is-visible"));
  window.setTimeout(() => {
    toast.classList.remove("is-visible");
    window.setTimeout(() => toast.remove(), 220);
  }, 3200);
};
