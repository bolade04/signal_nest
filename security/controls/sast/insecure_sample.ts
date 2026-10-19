// Inert negative-control fixture for the SAST gate. Never imported, built or executed.
import crypto from "crypto";

export function negativeControl(key: string): string {
  const cipher = crypto.createCipher("aes-256-cbc", key);
  return cipher.update("control", "utf8", "hex");
}
