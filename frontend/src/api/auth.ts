/** Signed-in state (UI-SHELL-1). The app never sees the token: only whether the cookie works. */
type Listener = () => void;

let signedOut = false;
const listeners = new Set<Listener>();

export const auth = {
  isSignedOut: (): boolean => signedOut,
  markSignedOut: (): void => {
    if (signedOut) return;
    signedOut = true;
    for (const l of listeners) l();
  },
  subscribe: (listener: Listener): (() => void) => {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  },
  /** Tests only. */
  reset: (): void => {
    signedOut = false;
  },
};
