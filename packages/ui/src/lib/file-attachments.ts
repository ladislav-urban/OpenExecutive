// Adding picked files to a message, within the limits the API gives
// (GET /chat/limits, the backend's own `_MAX_FILES_PER_TURN` and
// `_MAX_BYTES_PER_FILE`). The defaults are what the API sent last time it
// was asked, for the moment before its answer arrives; the server checks
// again either way. Tested in scripts/file-attachments.test.mjs.

export interface AttachmentLimits {
  max_files: number;
  max_bytes_per_file: number;
  /** For a file input's `accept`. */
  accept: string;
}

export const DEFAULT_ATTACHMENT_LIMITS: AttachmentLimits = {
  max_files: 5,
  max_bytes_per_file: 20 * 1024 * 1024,
  accept: "image/png,image/jpeg,image/gif,image/webp,.pdf,.docx,.doc,.xlsx,.xlsm,.txt,.md,.rst,.csv",
};

export interface FileSelection<T> {
  files: T[];
  rejected: string[];
}

type AttachmentFile = {
  name: string;
  size: number;
};

export function mergePickedFiles<T extends AttachmentFile>(
  current: readonly T[],
  picked: readonly T[],
  limits: Pick<AttachmentLimits, "max_files" | "max_bytes_per_file"> = DEFAULT_ATTACHMENT_LIMITS,
): FileSelection<T> {
  const files = [...current];
  const rejected: string[] = [];

  for (const file of picked) {
    if (file.size > limits.max_bytes_per_file) {
      const maxSizeMb = limits.max_bytes_per_file / (1024 * 1024);
      rejected.push(`${file.name} is too large (max ${maxSizeMb} MB)`);
      continue;
    }
    if (files.length >= limits.max_files) break;
    if (files.some((existing) => existing.name === file.name && existing.size === file.size)) {
      continue;
    }
    files.push(file);
  }

  return { files, rejected };
}
