import assert from "node:assert/strict";
import test from "node:test";
import { directoryRefusal, directorySubjects } from "../src/lib/directory.ts";

const SSO = "oidc";
const subjects = directorySubjects([
  { email: "Jane@Co.example", person_id: 2, directory_subject: "kc-jane" },
  { email: "pat@co.example", person_id: 1, directory_subject: null },
  { email: "odd@co.example", person_id: 3 },
  { person_id: 4, directory_subject: "kc-no-email" },
]);

test("only rows the directory manages are kept, by lowercased email", () => {
  assert.deepEqual([...subjects], [["jane@co.example", "kc-jane"]]);
  assert.equal(directorySubjects(null).size, 0);
  assert.equal(directorySubjects({ email: "x" }).size, 0);
});

test("a managed person signs in through the provider, as themselves", () => {
  assert.equal(directoryRefusal({ provider: SSO, email: "jane@co.example", sub: "kc-jane" }, subjects, SSO), null);
});

test("Google is refused for a managed email", () => {
  assert.equal(
    directoryRefusal({ provider: "google", email: "JANE@co.example", sub: "1234" }, subjects, SSO),
    "directory_managed",
  );
});

test("another identity holding a managed email is refused", () => {
  assert.equal(
    directoryRefusal({ provider: SSO, email: "jane@co.example", sub: "kc-someone-else" }, subjects, SSO),
    "directory_subject_mismatch",
  );
  assert.equal(directoryRefusal({ provider: SSO, email: "jane@co.example", sub: undefined }, subjects, SSO), "directory_subject_mismatch");
});

test("everyone else signs in as before, with Google too", () => {
  assert.equal(directoryRefusal({ provider: "google", email: "pat@co.example", sub: "1" }, subjects, SSO), null);
  assert.equal(directoryRefusal({ provider: SSO, email: "new@co.example", sub: "kc-new" }, subjects, SSO), null);
});
