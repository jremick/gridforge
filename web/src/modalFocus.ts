const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not(:disabled)",
  'input:not(:disabled):not([type="hidden"])',
  "select:not(:disabled)",
  "textarea:not(:disabled)",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

function isVisible(el: HTMLElement): boolean {
  if (el.hidden) return false;
  if (el.closest(".hidden")) return false;
  const style = window.getComputedStyle(el);
  return style.display !== "none" && style.visibility !== "hidden";
}

export class TrapFocusHelper {
  private readonly panel: HTMLElement;
  private previousFocus: HTMLElement | null = null;
  private active = false;

  constructor(panel: HTMLElement) {
    this.panel = panel;
  }

  private getFocusable(): HTMLElement[] {
    return Array.from(
      this.panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)
    ).filter(isVisible);
  }

  private handleKeyDown = (e: KeyboardEvent): void => {
    if (!this.active || e.key !== "Tab") return;
    const focusable = this.getFocusable();
    if (focusable.length === 0) {
      e.preventDefault();
      this.panel.focus();
      return;
    }

    const first = focusable[0]!;
    const last = focusable[focusable.length - 1]!;
    const activeEl = document.activeElement as HTMLElement | null;

    if (e.shiftKey) {
      if (!activeEl || activeEl === first || !this.panel.contains(activeEl)) {
        e.preventDefault();
        last.focus();
      }
    } else if (!activeEl || activeEl === last || !this.panel.contains(activeEl)) {
      e.preventDefault();
      first.focus();
    }
  };

  activate(initialFocus?: HTMLElement | null): void {
    if (!this.active) {
      this.previousFocus =
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      document.addEventListener("keydown", this.handleKeyDown, true);
      this.active = true;
    }

    if (!this.panel.hasAttribute("tabindex")) {
      this.panel.setAttribute("tabindex", "-1");
    }

    window.requestAnimationFrame(() => {
      if (!this.active) return;
      if (initialFocus && isVisible(initialFocus)) {
        initialFocus.focus();
        return;
      }
      const focusable = this.getFocusable();
      if (focusable[0]) {
        focusable[0].focus();
      } else {
        this.panel.focus();
      }
    });
  }

  deactivate(): void {
    if (!this.active) return;
    this.active = false;
    document.removeEventListener("keydown", this.handleKeyDown, true);
    const prev = this.previousFocus;
    this.previousFocus = null;
    if (prev && prev.isConnected) {
      prev.focus();
    }
  }
}
