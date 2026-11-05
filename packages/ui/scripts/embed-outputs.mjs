// The build outputs a host app checks itself against (docs/auth.md, "Host
// origins"), so its checks never need this app running or its secrets:
//
//   embed-manifest.json  the navigation (navConfig.ts's destinations and
//                        hubs, team and solo), the routes, the contract,
//                        the User Guide's section ids (/guide?section=<id>)
//                        and the pieces a host places on its own screens
//                        (the inline Approve, /embed/approve/{alertId}, and
//                        the one-pass sign-in, /signin/sso, and its
//                        sign-out, /signout/sso)
//   host-list.json       the calls a host origin's page may make
//
// The third, openapi.json, comes from the API (scripts/export-openapi.py).
// `make embed-outputs` writes all three.
//
//   node --experimental-strip-types scripts/embed-outputs.mjs <directory>
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { buildDestinations, buildHubs } from "../src/components/shell/navConfig.ts";
import { APP_ROUTES, EMBED_CONTRACT, EMBED_PROTOCOL, MAX_APPROVE_HEIGHT, THEME_TOKENS } from "../src/lib/embed.ts";
import { GUIDE_SECTION_IDS } from "../src/lib/guideSections.ts";

const here = path.dirname(fileURLToPath(import.meta.url));

const place = ({ href, label, description }) => ({ href, label, description });

export function embedManifest() {
  const pkg = JSON.parse(readFileSync(path.join(here, "../package.json"), "utf8"));
  const navigation = (mode) => ({
    destinations: buildDestinations({ mode }).map((d) => ({ key: d.key, ...place(d), alsoActiveOn: d.alsoActiveOn ?? [] })),
    hubs: buildHubs({ mode }).map((h) => ({ key: h.key, label: h.label, description: h.description, tabs: h.tabs.map(place) })),
  });
  return {
    version: pkg.version,
    protocol: EMBED_PROTOCOL,
    contract: EMBED_CONTRACT,
    themeTokens: [...THEME_TOKENS],
    routes: [...APP_ROUTES],
    navigation: { team: navigation("team"), solo: navigation("solo") },
    guideSections: [...GUIDE_SECTION_IDS],
    // app/embed/*: placed in a host's own screen, never shown as a page.
    pieces: {
      approve: { path: "/embed/approve/{alertId}", query: "?embed=1", maxHeight: MAX_APPROVE_HEIGHT },
      // Sign in and come back to the host's page in one pass (app/signin/sso).
      // A session for anyone but `hint` is ended and the sign-in runs (54.a).
      signIn: { path: "/signin/sso", query: "?to={hostPage}&hint={email}", failed: "oe_signin=failed", replacesOtherUser: true },
      // Sign out here on the way to the host's own sign-out (app/signout/sso);
      // a top-level navigation only.
      signOut: { path: "/signout/sso", query: "?to={hostPage}" },
    },
  };
}

export function hostList() {
  return JSON.parse(readFileSync(path.join(here, "../src/lib/host-list.json"), "utf8"));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const out = process.argv[2];
  if (!out) {
    console.error("usage: node --experimental-strip-types scripts/embed-outputs.mjs <directory>");
    process.exit(2);
  }
  mkdirSync(out, { recursive: true });
  writeFileSync(path.join(out, "embed-manifest.json"), JSON.stringify(embedManifest(), null, 2) + "\n");
  writeFileSync(path.join(out, "host-list.json"), JSON.stringify(hostList(), null, 2) + "\n");
}
