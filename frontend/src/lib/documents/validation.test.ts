import { describe, expect, it } from "vitest";

import { MAX_UPLOAD_BYTES, formatSize, validateFile } from "./validation";

function file(name: string, size = 10): File {
  return new File([new Uint8Array(size)], name);
}

describe("validateFile", () => {
  it.each(["a.pdf", "a.txt", "a.md", "a.markdown", "A.PDF", "informe final.Md"])("acepta %s", (name) => {
    expect(validateFile(file(name))).toBeNull();
  });

  it.each(["a.docx", "a.exe", "a", "pdf"])("rechaza el tipo de %s", (name) => {
    expect(validateFile(file(name))).toMatch(/no admitido/);
  });

  it("rechaza archivos vacíos", () => {
    expect(validateFile(file("a.txt", 0))).toMatch(/vacío/);
  });

  it("acepta el tamaño máximo exacto y rechaza uno más", () => {
    expect(validateFile(file("a.txt", MAX_UPLOAD_BYTES))).toBeNull();
    expect(validateFile(file("a.txt", MAX_UPLOAD_BYTES + 1))).toMatch(/máximo es 20 MB/);
  });
});

describe("formatSize", () => {
  it("usa la unidad adecuada", () => {
    expect(formatSize(512)).toBe("512 B");
    expect(formatSize(2048)).toBe("2.0 KB");
    expect(formatSize(5 * 1024 * 1024)).toBe("5.0 MB");
  });
});
