/** Shared AbortController lifecycle and abort-aware error reporting for Studio modals and drawers. */

export type ToastKind = "success" | "error" | "info";

/**
 * Link an optional parent/workspace AbortSignal into a component's own AbortController
 * so disposing either tears down in-flight requests and listeners cleanly.
 */
export function linkAbortSignal(
  lifecycle: AbortController,
  externalSignal?: AbortSignal
): AbortSignal {
  const signal = lifecycle.signal;
  if (externalSignal) {
    if (externalSignal.aborted) {
      lifecycle.abort();
    } else {
      externalSignal.addEventListener("abort", () => lifecycle.abort(), {
        once: true,
        signal,
      });
    }
  }
  return signal;
}

/** Configure standard accessible dialog backdrop classes and ARIA attributes on a modal root. */
export function initModalBackdrop(root: HTMLElement, titleId: string): void {
  root.className = "gf-modal-backdrop hidden";
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  root.setAttribute("aria-labelledby", titleId);
}

/** Report an async UI action failure unless the owning component/workspace has already aborted. */
export function reportAbortAwareError(
  signal: AbortSignal,
  err: unknown,
  fallbackMessage: string,
  onToast: (message: string, kind: ToastKind) => void,
  onApiError?: (err: unknown) => void
): void {
  if (signal.aborted) return;
  const msg = err instanceof Error ? err.message : fallbackMessage;
  onToast(msg, "error");
  onApiError?.(err);
}
