import assert from "node:assert/strict";
import test from "node:test";
import { DEFAULT_ATTACHMENT_LIMITS, mergePickedFiles } from "../src/lib/file-attachments.ts";

// The limits come from GET /chat/limits; these are the API's.
const MAX_BYTES_PER_FILE = DEFAULT_ATTACHMENT_LIMITS.max_bytes_per_file;
const MAX_FILES_PER_TURN = DEFAULT_ATTACHMENT_LIMITS.max_files;

test("rejects oversized files with an actionable message", () => {
  const oversized = { name: "quarterly-report.pdf", size: MAX_BYTES_PER_FILE + 1 };

  const result = mergePickedFiles([], [oversized]);

  assert.deepEqual(result.files, []);
  assert.deepEqual(result.rejected, ["quarterly-report.pdf is too large (max 20 MB)"]);
});

test("keeps valid files while rejecting oversized files from the same pick", () => {
  const valid = { name: "notes.txt", size: 100 };
  const oversized = { name: "archive.zip", size: MAX_BYTES_PER_FILE + 1 };

  const result = mergePickedFiles([], [valid, oversized]);

  assert.deepEqual(result.files, [valid]);
  assert.deepEqual(result.rejected, ["archive.zip is too large (max 20 MB)"]);
});

test("preserves the existing file-count cap and de-duplicates files", () => {
  const current = Array.from({ length: MAX_FILES_PER_TURN }, (_, i) => ({
    name: `file-${i}.txt`,
    size: i + 1,
  }));

  const result = mergePickedFiles(current, [current[0], { name: "extra.txt", size: 10 }]);

  assert.deepEqual(result.files, current);
  assert.deepEqual(result.rejected, []);
});

test("follows the limits the API gives", () => {
  const limits = { max_files: 1, max_bytes_per_file: 1024 * 1024 };
  const result = mergePickedFiles([], [{ name: "big.pdf", size: 2 * 1024 * 1024 }, { name: "a.txt", size: 10 }, { name: "b.txt", size: 10 }], limits);
  assert.deepEqual(result.files.map((f) => f.name), ["a.txt"]);
  assert.deepEqual(result.rejected, ["big.pdf is too large (max 1 MB)"]);
});
