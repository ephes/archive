for (const button of document.querySelectorAll("[data-copy-path]")) {
    button.addEventListener("click", async () => {
        const podcastUrl = new URL(button.dataset.copyPath, window.location.origin).href;
        const defaultLabel = button.dataset.defaultLabel || "Copy";
        const successLabel = button.dataset.successLabel || "Copied";

        try {
            await navigator.clipboard.writeText(podcastUrl);
            button.textContent = successLabel;
            window.setTimeout(() => {
                button.textContent = defaultLabel;
            }, 1500);
        } catch {
            window.prompt(`${defaultLabel}:`, podcastUrl);
        }
    });
}
