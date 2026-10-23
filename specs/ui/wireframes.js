(() => {
  "use strict";
  const status = document.querySelector("#demo-status");
  document.querySelectorAll("button[data-static]").forEach((button) => {
    button.addEventListener("click", () => {
      if (status) {
        status.textContent = `Static wireframe: “${button.textContent.trim()}” does not change data or call a service.`;
      }
    });
  });
})();
