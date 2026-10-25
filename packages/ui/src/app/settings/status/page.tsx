import { GOOGLE_SIGN_IN, LOCAL_LOGIN, OIDC, loadDirectorySubjects } from "@/auth";
import SetupStatusView from "@/components/settings/SetupStatusView";
import { parseAllowedEmails } from "@/lib/allowlist";
import { FALSEY_ENV } from "@/lib/localLogin";
import { issuerAllowed } from "@/lib/oidc";
import { directorySignInCheck, signInCheck } from "@/lib/setupStatus";

// Read this server's settings on every request. Prerendered, the page would
// show the settings of whichever machine ran `next build`.
export const dynamic = "force-dynamic";

export default async function SetupStatusPage() {
  const signIn = signInCheck({
    localLogin: LOCAL_LOGIN,
    googleClientId: process.env.AUTH_GOOGLE_ID,
    googleClientSecret: process.env.AUTH_GOOGLE_SECRET,
    oidcIssuer: process.env.AUTH_OIDC_ISSUER,
    oidcClientId: process.env.AUTH_OIDC_ID,
    oidcClientSecret: process.env.AUTH_OIDC_SECRET,
    oidcIssuerAllowed: issuerAllowed(process.env.AUTH_OIDC_ISSUER?.trim() ?? ""),
    oidcTrustUnverifiedEmail: OIDC?.trustUnverifiedEmail === true,
    allowedEmails: parseAllowedEmails(process.env.ALLOWED_EMAILS),
    authUrl: process.env.AUTH_URL,
    publicDeployment: !FALSEY_ENV.has((process.env.OE_PUBLIC_DEPLOYMENT ?? "").trim().toLowerCase()),
  });
  // The People the directory manages (only named while its sync is on).
  const subjects = LOCAL_LOGIN ? null : await loadDirectorySubjects();
  const directorySignIn = subjects
    ? directorySignInCheck(new Set(subjects.keys()), parseAllowedEmails(process.env.ALLOWED_EMAILS), GOOGLE_SIGN_IN)
    : null;
  return <SetupStatusView signIn={signIn} directorySignIn={directorySignIn} />;
}
