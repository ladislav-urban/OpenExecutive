// What /signin says for a refused or failed sign-in. Keyed on fixed values
// only: neither the error nor the reason from the address is ever shown.
// No imports, so `npm test` runs it directly (scripts/signinError.test.mjs).

/** The one reason /signin reads besides the error: the directory has this
 *  person on the way in (lib/directoryAdmit.ts). */
const DIRECTORY_WAITING = "directory_waiting";

export function describeError(code: string, reason?: string | null): string {
  switch (code) {
    case "AccessDenied":
      if (reason === DIRECTORY_WAITING) {
        return "We’re still setting up your access. Try again in a few minutes.";
      }
      return "That account can’t sign in here: it isn’t on the allow-list for this workspace, or its email isn’t verified. Ask an admin to add you.";
    case "CredentialsSignin":
      return "Open only works in a browser on the computer running Open Executive, at http://localhost:3000.";
    case "Configuration":
      return "Authentication is misconfigured. Contact the administrator.";
    default:
      return "Sign-in failed. Try again, or contact the administrator if this keeps happening.";
  }
}
